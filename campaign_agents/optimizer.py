"""Resource-aware portfolio search using the public ordering/capping rules."""
import time

import numpy as np

from .models import Arm, campaign_mask


def variants(profile, arm):
    """Full pilot segment and supported refinements for feasible allocation."""
    base = arm.campaign()
    group = profile.loc[campaign_mask(profile, base)]
    result = [base]
    for columns in [("data_segment",), ("call_segment",), ("data_segment", "call_segment")]:
        for keys, _ in group.groupby(list(columns), observed=True, sort=True):
            keys = keys if isinstance(keys, tuple) else (keys,)
            result.append({**base, **{f"filter_{column}": str(value) for column, value in zip(columns, keys)}})
    unique = {}
    for campaign in result:
        mask = campaign_mask(profile, campaign)
        indexes = np.flatnonzero(mask)
        if len(indexes):
            key = tuple(indexes)
            if key not in unique:
                unique[key] = (campaign, indexes, arm)
    return list(unique.values())


def effective_indexes(indexes, remaining_contacts, remaining_budget, price):
    n = min(len(indexes), 5000, max(0, int(remaining_contacts)))
    if price > 0:
        n = min(n, max(0, int((remaining_budget + 1e-9) // price)))
    return indexes[:n]


class Optimizer:
    def pilot_baseline(self, state):
        baseline = np.zeros(len(state.profile))
        arpu = state.profile["predicted_arpu"].to_numpy(dtype=float)
        for pilot in state.pilots:
            mask = campaign_mask(state.profile, pilot["filters"])
            size = int(mask.sum())
            if not size:
                continue
            probability = min(1.0, pilot["n_customers"] / size)
            observed = max(0.0, pilot["posterior_mean"]) * arpu[mask]
            baseline[mask] += probability * np.maximum(observed - baseline[mask], 0.0)
        return baseline

    def plan(self, state, budget, contacts, deadline):
        arpu = state.profile["predicted_arpu"].to_numpy(dtype=float)
        baseline = self.pilot_baseline(state)
        options = []
        for arm in state.arms:
            if arm.n and arm.mean - arm.standard_error > 0:
                options.extend(variants(state.profile, arm))
        best_plan, best_score = [], -float("inf")
        # A small deterministic sweep keeps free reach and costly channels in
        # competition; no solver dependency or exponential subset enumeration.
        for money_penalty in (0.0, 1.0, 3.0, 10.0):
            if time.monotonic() >= deadline:
                break
            remaining_budget, remaining_contacts = float(budget), int(contacts)
            covered = np.zeros(len(state.profile), dtype=bool)
            selected, conservative_total = [], 0.0
            for _ in range(10):
                winner = None
                winner_priority = 0.0
                for campaign, raw_indexes, arm in options:
                    price = float(state.channels[arm.channel]["cost_per_contact"])
                    indexes = effective_indexes(raw_indexes, remaining_contacts, remaining_budget, price)
                    if not len(indexes) or covered[indexes].any():
                        continue
                    cost = len(indexes) * price
                    lower = arm.mean - arm.standard_error
                    conservative = float(np.maximum(lower * arpu[indexes] - baseline[indexes], 0.0).sum() - cost)
                    priority = conservative - money_penalty * cost
                    if priority > winner_priority:
                        mean_gain = float(np.maximum(arm.mean * arpu[indexes] - baseline[indexes], 0.0).sum() - cost)
                        winner = (campaign, raw_indexes, indexes, arm, cost, conservative, mean_gain)
                        winner_priority = priority
                if winner is None:
                    break
                campaign, raw_indexes, indexes, arm, cost, conservative, mean_gain = winner
                covered[indexes] = True
                remaining_budget -= cost
                remaining_contacts -= len(indexes)
                conservative_total += conservative
                reason = "Положительная оценка с запасом на шум пилота; учтены стоимость и пересечение с пилотами."
                if len(indexes) < len(raw_indexes):
                    reason += " Охват ограничен публичным порядком ID и остатком ресурсов."
                if any(key not in arm.filters for key in campaign if key.startswith("filter_")):
                    reason += " Оценка подгруппы перенесена с пилота более широкого сегмента."
                selected.append({**campaign, "campaign_name": f"campaign_{len(selected) + 1:02d}",
                                 "expected_contacts": int(len(indexes)), "eligible_customers": int(len(raw_indexes)),
                                 "expected_cost": float(cost), "expected_net_gain": mean_gain,
                                 "estimated_lift": float(arm.mean), "standard_error": float(arm.standard_error),
                                 "reason": reason})
            if conservative_total > best_score and selected:
                best_plan, best_score = selected, conservative_total
        if best_plan:
            state.decisions.append(f"Выбрано {len(best_plan)} кампаний без повторов среди финальных контактов.")
            return best_plan
        return self.fallback(state, budget, contacts)

    def fallback(self, state, budget, contacts):
        state.warnings.append("Нет убедительно прибыльного допустимого плана. Возвращается минимальная доступная экспозиция; прибыль не гарантируется.")
        if contacts <= 0 or state.profile.empty or not state.channels:
            return []
        channel = min(state.channels, key=lambda name: (state.channels[name]["cost_per_contact"], name))
        price = float(state.channels[channel]["cost_per_contact"])
        choices = []
        seen = set()
        # Use all proposed cells, even if no valid pilot observation was obtained.
        for original in state.arms:
            key = (original.cell, original.target)
            if key in seen:
                continue
            seen.add(key)
            observed_same_channel = next((arm for arm in state.arms if arm.cell == original.cell and arm.target == original.target and arm.channel == channel and arm.n), None)
            arm = observed_same_channel or Arm(dict(original.filters), original.target, channel, original.size, original.arpu_sum)
            for campaign, raw_indexes, _ in variants(state.profile, arm):
                indexes = effective_indexes(raw_indexes, contacts, budget, price)
                if len(indexes):
                    mean = arm.mean if arm.n else 0.0
                    risk = (mean - arm.standard_error) * float(state.profile.iloc[indexes]["predicted_arpu"].sum()) - len(indexes) * price
                    choices.append((len(indexes), -risk, campaign, indexes, raw_indexes, arm))
        if not choices:
            return []
        _, _, campaign, indexes, raw_indexes, arm = min(choices, key=lambda choice: (choice[0], choice[1], str(sorted(choice[2].items()))))
        mean = arm.mean if arm.n else 0.0
        return [{**campaign, "campaign_name": "fallback_minimum_exposure",
                 "expected_contacts": int(len(indexes)), "eligible_customers": int(len(raw_indexes)),
                 "expected_cost": float(len(indexes) * price),
                 "expected_net_gain": float(mean * state.profile.iloc[indexes]["predicted_arpu"].sum() - len(indexes) * price),
                 "estimated_lift": float(mean), "standard_error": float(arm.standard_error),
                 "reason": "Минимальный охват через самый дешёвый канал для выполнения требования 1–10 кампаний. Оценка при отсутствии пилота неизвестна."}]
