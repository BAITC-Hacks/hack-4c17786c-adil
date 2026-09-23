"""Adaptive, bounded exploration. Only env.run_pilot reveals effects."""
import math
import time

import numpy as np

from .models import Arm


class Experimenter:
    def __init__(self, max_pilots=20):
        self.max_pilots = min(20, max(0, int(max_pilots)))

    def choose(self, state, step):
        active = [arm for arm in state.arms if not arm.unavailable]
        if not active:
            return None, "Нет доступных гипотез"
        observed = [arm for arm in active if arm.n > 0]
        visited = {arm.cell for arm in observed}
        fresh_cells = [arm for arm in active if arm.n == 0 and arm.cell not in visited]
        # First cover multiple cells, then alternate discovery and confirmation.
        if len(visited) < min(8, len({arm.cell for arm in active})) and fresh_cells:
            return max(fresh_cells, key=lambda arm: arm.arpu_sum * (0.10 + max(arm.prior_mean, 0))), "Первичная проверка нового сегмента"

        # Measure a channel directly; never assume a linear transfer of observed
        # effect because channel conversion may saturate.
        if observed and step % 4 == 2:
            promising = sorted(observed, key=lambda arm: -(arm.mean - arm.standard_error) * arm.arpu_sum)
            for best in promising:
                if best.mean <= best.standard_error:
                    continue
                for channel in sorted(state.channels, key=lambda name: (state.channels[name]["cost_per_contact"], name)):
                    if channel == best.channel:
                        continue
                    if any(arm.cell == best.cell and arm.target == best.target and arm.channel == channel for arm in state.arms):
                        continue
                    cost = state.channels[channel]["cost_per_contact"]
                    if cost * min(best.size, 100) > min(8000, state.initial_budget * 0.08):
                        continue
                    arm = Arm(dict(best.filters), best.target, channel, best.size, best.arpu_sum, prior_mean=0.0)
                    state.arms.append(arm)
                    return arm, "Проверка альтернативного канала для перспективного перехода"

        # Every other decision tests a new direction. Favor alternatives in
        # productive cells, but keep exploration of unvisited cells possible.
        if step % 2 == 0:
            fresh = [arm for arm in active if arm.n == 0]
            if fresh:
                def potential(arm):
                    same_cell = [item for item in observed if item.cell == arm.cell]
                    evidence = max((item.mean for item in same_cell), default=0.0)
                    return arm.arpu_sum * (0.10 + max(arm.prior_mean, 0) + 0.20 * max(evidence, 0))
                return max(fresh, key=potential), "Разведка альтернативной гипотезы"

        uncertain = [arm for arm in observed if arm.n < 600 and arm.trials < 4 and arm.mean + 1.5 * arm.standard_error > 0]
        if uncertain:
            def information(arm):
                borderline = math.exp(-0.5 * (arm.mean / max(arm.standard_error, 1e-6)) ** 2)
                return arm.arpu_sum * arm.standard_error * (0.25 + borderline) * (1 + max(arm.mean, 0))
            return max(uncertain, key=information), "Повторная проверка для снижения неопределённости"
        fresh = [arm for arm in active if arm.n == 0]
        if fresh:
            return max(fresh, key=lambda arm: arm.arpu_sum * (0.10 + max(arm.prior_mean, 0))), "Проверка ещё одного направления"
        return None, "Дальнейшая проверка не приоритетна"

    def run(self, env, state, deadline):
        spent = 0.0
        pilot_budget = min(state.initial_budget * 0.20, 20000.0)
        min_final_size = min((arm.size for arm in state.arms), default=1)
        contact_reserve = max(min_final_size, int(state.initial_contacts * 0.65))
        for step in range(min(self.max_pilots, int(env.pilots_left))):
            if time.monotonic() >= deadline:
                state.warnings.append("Разведка остановлена по лимиту времени.")
                break
            if env.pilots_left <= 0 or env.remaining_contacts <= contact_reserve:
                break
            arm, reason = self.choose(state, step)
            if arm is None:
                break
            price = float(state.channels[arm.channel]["cost_per_contact"])
            size = 100 if arm.n == 0 else (200 if arm.mean < 2 * arm.standard_error else 150)
            available = max(0, int(env.remaining_contacts) - contact_reserve)
            if price > 0:
                available = min(available, int(max(0, min(env.remaining_budget, pilot_budget - spent)) // price))
            n = min(size, arm.size, available, 200)
            if n < 10:
                arm.unavailable = True
                # Allow inexpensive discovery after a paid channel exhausts its
                # exploration allocation, without reading any hidden state.
                free = next((name for name, data in state.channels.items() if data["cost_per_contact"] == 0), None)
                if free and arm.channel != free and not any(item.cell == arm.cell and item.target == arm.target and item.channel == free for item in state.arms):
                    state.arms.append(Arm(dict(arm.filters), arm.target, free, arm.size, arm.arpu_sum))
                continue
            budget_before = float(env.remaining_budget)
            try:
                result = env.run_pilot(target_tariff=arm.target, channel=arm.channel, n_customers=int(n), **arm.filters)
                actual = int(result["n_customers"])
                ratio = float(result["observed_lift_ratio"])
                cost = float(result["cost"])
                if actual < 1 or actual > n or not np.isfinite(cost) or cost < 0:
                    raise ValueError("Invalid pilot resource accounting")
                arm.observe(ratio, actual)
                state.pilots.append({
                    "target_tariff": arm.target, "channel": arm.channel,
                    "n_customers": actual, "cost": cost,
                    "observed_lift_ratio": ratio, "filters": dict(arm.filters),
                    "posterior_mean": arm.mean, "standard_error": arm.standard_error,
                    "reason": reason,
                })
            except Exception as exc:
                # A failed observation may still have consumed resources. Read
                # actual public remainders instead of rolling them back locally.
                arm.unavailable = True
                state.warnings.append(f"Пилот {arm.target}/{arm.channel}: {type(exc).__name__}; использована запасная логика.")
            spent += max(0.0, budget_before - float(env.remaining_budget))
        state.decisions.append(f"Получено {len(state.pilots)} валидных наблюдений; оценки обновлены с учётом размера выборки.")
