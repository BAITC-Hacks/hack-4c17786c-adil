"""Build weak historical priors and a diverse finite set of public actions."""
from pathlib import Path

import numpy as np
import pandas as pd

from .models import Arm


class Analyst:
    def __init__(self, history_path):
        self.history_path = Path(history_path)

    def historical_priors(self, warnings):
        columns = ["AVG_ARPU_PREV_3M", "AVG_ARPU_NEXT_3M", "tariff_plan_code_from", "tariff_plan_code_to"]
        try:
            history = pd.read_csv(self.history_path, usecols=columns)
            before = pd.to_numeric(history[columns[0]], errors="coerce")
            after = pd.to_numeric(history[columns[1]], errors="coerce")
            valid = np.isfinite(before) & np.isfinite(after) & (before > 0)
            history = history.loc[valid].copy()
            before, after = before.loc[valid], after.loc[valid]
            history["segment"] = np.where(before < 1000, "LOW", np.where(before > 5000, "HIGH", "MID"))
            # Observational changes rank hypotheses only; they do not estimate
            # campaign conversion. Winsorization prevents near-zero denominators
            # from dominating the ranking.
            history["change"] = ((after - before) / before).clip(-1.0, 2.0)
            grouped = history.groupby(["tariff_plan_code_from", "segment", "tariff_plan_code_to"], observed=True)["change"].agg(["median", "count"])
            return {key: float(np.clip(row["median"] * row["count"] / (row["count"] + 12) * 0.12, -0.10, 0.18)) for key, row in grouped.iterrows()}
        except (OSError, ValueError, KeyError, pd.errors.ParserError) as exc:
            warnings.append(f"История недоступна ({type(exc).__name__}); гипотезы строятся по профилям и тарифам.")
            return {}

    def propose(self, state, tariff_frame):
        priors = self.historical_priors(state.warnings)
        prices = {}
        if "price_tariff" in tariff_frame:
            for _, row in tariff_frame.iterrows():
                try:
                    value = float(row["price_tariff"])
                    if np.isfinite(value):
                        prices[str(row["tariff_plan_code"])] = value
                except (TypeError, ValueError):
                    pass
        channels = sorted(state.channels, key=lambda name: (state.channels[name]["cost_per_contact"], name))
        primary = "sms" if "sms" in channels and state.initial_budget >= 400 else channels[0]
        groups = state.profile.groupby(["current_tariff", "arpu_segment"], observed=True, sort=True)
        arms = []
        for (current, segment), group in groups:
            filters = {"filter_current_tariff": str(current), "filter_arpu_segment": str(segment)}
            cells = [(filters, group)]
            # Split oversized cells with filters supported by the final contract.
            for column, key in [("data_segment", "filter_data_segment"), ("call_segment", "filter_call_segment")]:
                next_cells = []
                for cell_filters, cell in cells:
                    if len(cell) <= 5000:
                        next_cells.append((cell_filters, cell))
                    else:
                        next_cells.extend(({**cell_filters, key: str(value)}, subset) for value, subset in cell.groupby(column, observed=True, sort=True))
                cells = next_cells
            for cell_filters, cell in cells:
                if len(cell) == 0:
                    continue
                avg = float(cell["predicted_arpu"].mean())
                ranked = []
                for target in sorted(state.tariffs):
                    if target == str(current):
                        continue
                    # Weak product-price heuristic only when history is absent.
                    price = prices.get(target, avg)
                    price_hint = 0.02 * np.tanh((price - avg) / max(avg, 1.0))
                    prior = priors.get((str(current), str(segment), target), float(price_hint))
                    ranked.append((prior, target))
                ranked.sort(key=lambda item: (-item[0], item[1]))
                for prior, target in ranked[:4]:
                    arms.append(Arm(cell_filters, target, primary, len(cell), float(cell["predicted_arpu"].sum()), prior_mean=prior))
        state.arms = sorted(arms, key=lambda arm: (-arm.arpu_sum * (0.08 + max(arm.prior_mean, 0)), arm.key))
        state.decisions.append(f"Сформировано {len(state.arms)} гипотез; история используется как слабое начальное предположение.")
        return state.arms
