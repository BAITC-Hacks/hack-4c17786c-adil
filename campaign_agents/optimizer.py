"""Resource-aware portfolio search using the public ordering/capping rules."""
import time

import numpy as np

from .models import Arm, campaign_mask


def variants(profile, arm, deadline=float("inf")):
    """Full pilot segment and supported refinements for feasible allocation."""
    base = arm.campaign()
    group = profile.loc[campaign_mask(profile, base)]
    result = [base]
    for columns in [("data_segment",), ("call_segment",), ("data_segment", "call_segment")]:
        if time.monotonic() >= deadline:
            break
        for keys, _ in group.groupby(list(columns), observed=True, sort=True):
            if time.monotonic() >= deadline:
                break
            keys = keys if isinstance(keys, tuple) else (keys,)
            result.append({**base, **{f"filter_{column}": str(value) for column, value in zip(columns, keys)}})
    unique = {}
    for campaign in result:
        if time.monotonic() >= deadline:
            break
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
    def pilot_baseline(self, state, deadline=float("inf")):
        baseline = np.zeros(len(state.profile))
        arpu = state.profile["predicted_arpu"].to_numpy(dtype=float)
        for pilot in state.pilots:
            if time.monotonic() >= deadline:
                break
            mask = campaign_mask(state.profile, pilot["filters"])
            size = int(mask.sum())
            if not size:
                continue
            probability = min(1.0, pilot["n_customers"] / size)
            observed = max(0.0, pilot["posterior_mean"]) * arpu[mask]
            baseline[mask] += probability * np.maximum(observed - baseline[mask], 0.0)
        return baseline

    def plan(self, state, budget, contacts, deadline):
        if time.monotonic() >= deadline:
            return self.fallback(state, budget, contacts, deadline)
        arpu = state.profile["predicted_arpu"].to_numpy(dtype=float)
        baseline = self.pilot_baseline(state, deadline)
        options = []
        for arm in state.arms:
            if time.monotonic() >= deadline:
                break
            if arm.n and arm.mean - arm.standard_error > 0:
                options.extend(variants(state.profile, arm, deadline))
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
                if time.monotonic() >= deadline:
                    break
                winner = None
                winner_priority = 0.0
                for campaign, raw_indexes, arm in options:
                    if time.monotonic() >= deadline:
                        break
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
            if time.monotonic() >= deadline:
                state.warnings.append("Поиск остановлен по лимиту времени; сохранён уже найденный допустимый план.")
            state.decisions.append(f"Выбрано {len(best_plan)} кампаний без повторов среди финальных контактов.")
            return best_plan
        return self.fallback(state, budget, contacts, deadline)

    def fallback(self, state, budget, contacts, deadline=float("inf")):
        state.warnings.append("Нет убедительно прибыльного допустимого плана. Возвращается резервный план с ограниченным охватом; прибыль не гарантируется.")
        if contacts <= 0 or state.profile.empty or not state.channels:
            return []
        channel = min(state.channels, key=lambda name: (state.channels[name]["cost_per_contact"], name))
        price = float(state.channels[channel]["cost_per_contact"])
        choices = []
        seen = set()
        # Use all proposed cells, even if no valid pilot observation was obtained.
        for original in state.arms:
            if time.monotonic() >= deadline:
                break
            key = (original.cell, original.target)
            if key in seen:
                continue
            seen.add(key)
            observed_same_channel = next((arm for arm in state.arms if arm.cell == original.cell and arm.target == original.target and arm.channel == channel and arm.n), None)
            arm = observed_same_channel or Arm(dict(original.filters), original.target, channel, original.size, original.arpu_sum)
            for campaign, raw_indexes, _ in variants(state.profile, arm, deadline):
                if time.monotonic() >= deadline:
                    break
                indexes = effective_indexes(raw_indexes, contacts, budget, price)
                if len(indexes):
                    mean = arm.mean if arm.n else 0.0
                    risk = (mean - arm.standard_error) * float(state.profile.iloc[indexes]["predicted_arpu"].sum()) - len(indexes) * price
                    choices.append((len(indexes), -risk, campaign, indexes, raw_indexes, arm))
        deadline_reached = time.monotonic() >= deadline
        if deadline_reached:
            state.warnings.append("Поиск остановлен по лимиту времени; резервный план не гарантирует минимальный охват среди всех гипотез.")
        if not choices:
            # After the deadline, complete only one public filter operation and
            # one refinement of the first proposed cell. Never restart the
            # full arms × variants search just to satisfy the 1–10 contract.
            if not state.arms:
                return []
            original = state.arms[0]
            arm = next((item for item in state.arms if item.cell == original.cell and item.target == original.target and item.channel == channel and item.n), None)
            arm = arm or Arm(dict(original.filters), original.target, channel, original.size, original.arpu_sum)
            campaign = arm.campaign()
            group = state.profile.loc[campaign_mask(state.profile, campaign)]
            groups = group.groupby(["data_segment", "call_segment"], observed=True, sort=True)
            sizes = groups.size()
            raw_indexes = group.index.to_numpy(dtype=int)
            # Missing optional refinement values must not discard a valid base
            # campaign; its current-tariff/ARPU filters remain sufficient.
            if not sizes.empty:
                data_segment, call_segment = sizes.idxmin()
                campaign.update(filter_data_segment=str(data_segment), filter_call_segment=str(call_segment))
                raw_indexes = groups.get_group((data_segment, call_segment)).index.to_numpy(dtype=int)
            indexes = effective_indexes(raw_indexes, contacts, budget, price)
            if not len(indexes):
                return []
            choices.append((len(indexes), 0.0, campaign, indexes, raw_indexes, arm))
        _, _, campaign, indexes, raw_indexes, arm = min(choices, key=lambda choice: (choice[0], choice[1], str(sorted(choice[2].items()))))
        mean = arm.mean if arm.n else 0.0
        return [{**campaign, "campaign_name": "fallback_minimum_exposure",
                 "expected_contacts": int(len(indexes)), "eligible_customers": int(len(raw_indexes)),
                 "expected_cost": float(len(indexes) * price),
                 "expected_net_gain": float(mean * state.profile.iloc[indexes]["predicted_arpu"].sum() - len(indexes) * price),
                 "estimated_lift": float(mean), "standard_error": float(arm.standard_error),
                 "reason": ("Ограниченный по времени резервный поиск; " if deadline_reached else "Минимальный охват; ")
                           + "самый дешёвый канал для выполнения требования 1–10 кампаний. Оценка при отсутствии пилота неизвестна."}]
