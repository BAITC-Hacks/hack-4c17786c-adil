"""Submission entry point: an offline, adaptive campaign planning system.

The agent accesses only public environment attributes and run_pilot().
It never imports evaluator or mock-model internals.
"""
from pathlib import Path
import time

import numpy as np
import pandas as pd

from campaign_agents.analyst import Analyst
from campaign_agents.experimenter import Experimenter
from campaign_agents.models import State
from campaign_agents.optimizer import Optimizer
from campaign_agents.validator import Validator


class Agent:
    def __init__(self, seed=42, history_path=None, max_pilots=20, time_limit_seconds=240):
        self.seed = int(seed)  # Current decision policy is deterministic.
        self.history_path = Path(history_path) if history_path is not None else Path(__file__).parent / "data" / "change_tariff.csv"
        self.max_pilots = max_pilots
        self.time_limit_seconds = max(0.1, min(float(time_limit_seconds), 280.0))
        self.last_report = {}

    def act(self, env):
        started = time.monotonic()
        deadline = started + self.time_limit_seconds
        self.last_report = {}
        profile = env.customer_profile.copy(deep=True)
        warnings = []
        required = ["ID_NUMBER", "current_tariff", "arpu_segment", "data_segment", "call_segment", "predicted_arpu"]
        missing = [column for column in required if column not in profile]
        if missing:
            self.last_report = {"warnings": ["Отсутствуют обязательные колонки: " + ", ".join(missing)], "campaigns": [], "pilots": [], "runtime_seconds": time.monotonic() - started}
            return []
        profile = profile.sort_values("ID_NUMBER", kind="stable").reset_index(drop=True)
        numeric = pd.to_numeric(profile["predicted_arpu"], errors="coerce")
        if not np.isfinite(numeric).all() or (numeric < 0).any():
            warnings.append("Некорректные значения predicted_arpu заменены нулём только для прогнозов.")
        profile["predicted_arpu"] = numeric.where(np.isfinite(numeric) & (numeric >= 0), 0.0)
        channels = {}
        for name, values in sorted(env.channels.items()):
            try:
                price = float(values["cost_per_contact"])
                if np.isfinite(price) and price >= 0:
                    channels[name] = {"cost_per_contact": price}
            except (TypeError, ValueError, KeyError):
                warnings.append(f"Пропущен некорректный канал {name}.")
        budget = max(0.0, float(env.remaining_budget))
        contacts = max(0, int(env.remaining_contacts))
        initial_pilots_left = int(env.pilots_left)
        state = State(profile, set(env.tariffs["tariff_plan_code"].astype(str)), channels, budget, contacts, warnings=warnings)
        records = []
        if profile.empty or contacts == 0 or not channels:
            state.warnings.append("Нет доступной аудитории, контактов или каналов: допустимая кампания невозможна.")
        else:
            Analyst(self.history_path).propose(state, env.tariffs)
            if not state.arms:
                state.warnings.append("Нет допустимого перехода на другой тариф.")
            else:
                Experimenter(self.max_pilots).run(env, state, deadline - min(10.0, self.time_limit_seconds * 0.1))
                if not state.pilots:
                    state.warnings.append("Валидные пилоты недоступны; результат является резервным планом.")
                records = Optimizer().plan(state, float(env.remaining_budget), int(env.remaining_contacts), deadline)
        campaigns, records = Validator().validate(records, state, float(env.remaining_budget), int(env.remaining_contacts))
        final_cost = float(sum(row["expected_cost"] for row in records))
        final_contacts = int(sum(row["expected_contacts"] for row in records))
        self.last_report = {
            "overview": {"customers": int(len(profile)), "baseline_arpu": float(profile["predicted_arpu"].sum()),
                         "candidate_count": len(state.arms), "observed_arms": sum(arm.n > 0 for arm in state.arms)},
            "pilots": state.pilots, "campaigns": records,
            "resources": {"initial_budget": budget, "remaining_budget": float(env.remaining_budget),
                          "initial_contacts": contacts, "remaining_contacts": int(env.remaining_contacts),
                          "pilots_used": initial_pilots_left - int(env.pilots_left), "pilots_left": int(env.pilots_left),
                          "final_cost": final_cost, "final_contacts": final_contacts,
                          "projected_remaining_budget": float(env.remaining_budget) - final_cost,
                          "projected_remaining_contacts": int(env.remaining_contacts) - final_contacts},
            "decisions": state.decisions, "warnings": state.warnings,
            "assumptions": [
                "Исторические переходы другой выборки используются только для слабого начального ранжирования.",
                "Шум пилота: опубликованное стандартное отклонение 0.804 / sqrt(n); модель нормального среднего является приближением.",
                "Интервал оценки описывает шум пилота, но не гарантирует перенос эффекта на более узкие подгруппы.",
                "ID пилотных контактов не доступны. Пересечение с пилотами учтено приближённо по вероятности отбора; финальные аудитории не пересекаются.",
                "expected_net_gain — прогноз дополнительного эффекта финальных кампаний после пилотов, а не результат судейства.",
            ],
            "runtime_seconds": time.monotonic() - started,
        }
        return campaigns
