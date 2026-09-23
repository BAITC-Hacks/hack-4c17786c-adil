"""Agent behavior against a small implementation of the PUBLIC environment API.

The deterministic observations here are test fixtures, not the organizer's
effect model. No test imports or inspects mock_environment internals.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from agent import Agent
from campaign_agents.models import Arm, State
from campaign_agents.optimizer import Optimizer, effective_indexes
from scoring_core import CHANNELS, FILTER_VALUES, apply_filters, validate_strategy


FILTER_COLUMNS = tuple(FILTER_VALUES) + ("filter_current_tariff",)
MISSING_HISTORY = Path(__file__).parent / "nonexistent_public_history.csv"


def make_profile(n_per_segment=1800):
    rows = []
    for i, arpu in enumerate(("LOW", "MID", "HIGH")):
        for j in range(n_per_segment):
            rows.append({
                "ID_NUMBER": 1 + i * n_per_segment + j,
                "current_tariff": "source",
                "arpu_segment": arpu,
                "data_segment": ("NON_USER", "LITE", "HEAVY")[j % 3],
                "call_segment": ("LOW", "MEDIUM", "HIGH")[j % 3],
                "predicted_arpu": (1200.0, 3200.0, 7200.0)[i],
                "ARPU_current": (1200.0, 3200.0, 7200.0)[i],
                "ARPU_3m_avg": (1200.0, 3200.0, 7200.0)[i],
                "DATA_VOLUME": float(2000 + 500 * (j % 3)),
                "OUT_LOC_ONNET_MIN": 75.0,
                "OUT_LOC_OFFNET_MIN": 30.0,
            })
    return pd.DataFrame(rows)


def make_tariffs():
    return pd.DataFrame([
        {"tariff_plan_code": code, "price_tariff": price,
         "Data_in_PKG": data, "Min_another_operator_in_PKG": 100,
         "Min_another_operator_and_city_in_PKG": 0}
        for code, price, data in [
            ("source", 2500.0, 2048),
            ("offer_a", 4500.0, 8192),
            ("offer_b", 4500.0, 8192),
        ]
    ])


class PublicEnvironment:
    """Resource-consuming pilot fixture with only documented public attributes."""

    def __init__(self, winner="offer_a", profile=None, budget=100000,
                 contacts=15000, malformed=None, pilots_left=20):
        self.customer_profile = make_profile() if profile is None else profile.copy()
        self.tariffs = make_tariffs()
        self.channels = {key: dict(value) for key, value in CHANNELS.items()}
        self.total_budget = budget
        self.max_total_contacts = contacts
        self.remaining_budget = budget
        self.remaining_contacts = contacts
        self.pilots_left = pilots_left
        self.pilot_history = []
        self.requests = []
        self.winner = winner
        self.malformed = malformed

    def run_pilot(self, target_tariff, channel, n_customers=100,
                  filter_arpu_segment=None, filter_data_segment=None,
                  filter_call_segment=None, filter_current_tariff=None):
        request = dict(target_tariff=target_tariff, channel=channel,
                       n_customers=n_customers,
                       filter_arpu_segment=filter_arpu_segment,
                       filter_data_segment=filter_data_segment,
                       filter_call_segment=filter_call_segment,
                       filter_current_tariff=filter_current_tariff)
        self.requests.append(request)
        if isinstance(self.malformed, type) and issubclass(self.malformed, Exception):
            raise self.malformed("Synthetic pilot service failure")
        if self.pilots_left <= 0:
            raise RuntimeError("No pilots remain")
        if target_tariff not in set(self.tariffs.tariff_plan_code):
            raise ValueError("Unknown target")
        if channel not in self.channels:
            raise ValueError("Unknown channel")
        segment = apply_filters(self.customer_profile, pd.Series(request))
        cost_per_contact = self.channels[channel]["cost_per_contact"]
        affordable = self.remaining_contacts
        if cost_per_contact > 0:
            affordable = min(affordable, int(self.remaining_budget // cost_per_contact))
        n = int(min(np.clip(n_customers, 10, 200), len(segment), affordable))
        if n <= 0:
            raise RuntimeError("No eligible contacts")
        cost = n * cost_per_contact
        self.remaining_budget -= cost
        self.remaining_contacts -= n
        self.pilots_left -= 1
        # An intentionally decisive observation makes the causal requirement
        # independent of the optimizer's exact confidence-bound formula.
        lift = 0.9 if target_tariff == self.winner else -0.9
        result = {
            "pilot": f"pilot_{len(self.pilot_history) + 1}",
            "target_tariff": target_tariff, "channel": channel,
            "n_customers": n, "cost": cost,
            "observed_lift_ratio": lift,
            "observed_lift_total": lift * n * float(segment.predicted_arpu.mean()),
            "remaining_budget": self.remaining_budget,
            "remaining_contacts": self.remaining_contacts,
        }
        if self.malformed == "missing_ratio":
            result.pop("observed_lift_ratio")
        elif self.malformed in ("nan", "inf"):
            result["observed_lift_ratio"] = float(self.malformed)
        self.pilot_history.append(dict(result))
        return result


class AgentContractTests(unittest.TestCase):
    def make_agent(self, **kwargs):
        return Agent(seed=42, history_path=MISSING_HISTORY, **kwargs)

    def assert_plan_valid(self, plan, env, allow_empty=False):
        self.assertIsInstance(plan, list)
        self.assertLessEqual(len(plan), 10)
        if not allow_empty:
            self.assertGreaterEqual(len(plan), 1)
        if not plan:
            return
        validate_strategy(pd.DataFrame(plan), env.tariffs)
        allowed_columns = set(FILTER_COLUMNS) | {"campaign_name", "target_tariff", "channel"}
        remaining_budget = env.remaining_budget
        remaining_contacts = env.remaining_contacts
        final_ids = set()
        for campaign in plan:
            self.assertLessEqual(set(campaign), allowed_columns,
                                 "Submission only retains documented campaign columns")
            segment = apply_filters(env.customer_profile, pd.Series(campaign)).sort_values("ID_NUMBER")
            cost = env.channels[campaign["channel"]]["cost_per_contact"]
            n = min(len(segment), 5000, remaining_contacts)
            if cost > 0:
                n = min(n, int(remaining_budget // cost))
            self.assertGreater(n, 0, "Returned campaigns must still have usable resources")
            picked = set(segment.head(n).ID_NUMBER)
            self.assertFalse(final_ids & picked,
                             "Final campaigns should not pay for duplicate audience exposure")
            final_ids.update(picked)
            remaining_contacts -= n
            remaining_budget -= n * cost
        self.assertGreaterEqual(remaining_contacts, 0)
        self.assertGreaterEqual(remaining_budget, 0)
        for request in env.requests:
            self.assertGreaterEqual(request["n_customers"], 10)
            self.assertLessEqual(request["n_customers"], 200)
        self.assertLessEqual(len(env.requests), 20)

    def assert_finite_report(self, agent):
        self.assertIsInstance(agent.last_report, dict)
        self.assertTrue(agent.last_report)
        json.dumps(agent.last_report, allow_nan=False)

    def test_pilot_observations_change_selected_target(self):
        plans = []
        for winner in ("offer_a", "offer_b"):
            with self.subTest(winner=winner):
                env = PublicEnvironment(winner=winner)
                agent = self.make_agent()
                plan = agent.act(env)
                self.assertGreater(len(env.pilot_history), 0)
                self.assert_plan_valid(plan, env)
                self.assertIn(winner, {item["target_tariff"] for item in plan})
                winning_coverage = sum(
                    min(5000, len(apply_filters(env.customer_profile, pd.Series(item))))
                    for item in plan if item["target_tariff"] == winner
                )
                losing_coverage = sum(
                    min(5000, len(apply_filters(env.customer_profile, pd.Series(item))))
                    for item in plan if item["target_tariff"] != winner
                )
                self.assertGreater(winning_coverage, losing_coverage)
                self.assert_finite_report(agent)
                plans.append(plan)
        self.assertNotEqual(plans[0], plans[1])

    def test_limits_include_pilot_cost_and_contact_spend(self):
        for contacts in (9000, 1750):
            with self.subTest(contacts=contacts):
                env = PublicEnvironment(profile=make_profile(8000), budget=1200, contacts=contacts)
                original = env.customer_profile.copy(deep=True)
                agent = self.make_agent()
                plan = agent.act(env)
                self.assert_plan_valid(plan, env)
                if contacts == 9000:
                    self.assertGreater(len(env.pilot_history), 0)
                    self.assertLess(env.remaining_budget, env.total_budget)
                    self.assertLess(env.remaining_contacts, env.max_total_contacts)
                self.assertGreater(env.remaining_contacts, 0,
                                   "Reserve contacts for the final campaign plan")
                self.assertGreaterEqual(env.remaining_budget, 0)
                pd.testing.assert_frame_equal(original, env.customer_profile)
                self.assert_finite_report(agent)

    def test_reusing_agent_resets_episode_state(self):
        agent = self.make_agent()
        env_a, env_b = PublicEnvironment(), PublicEnvironment()
        first = agent.act(env_a)
        first_report = json.dumps(agent.last_report, sort_keys=True, allow_nan=False)
        second = agent.act(env_b)
        self.assertEqual(first, second)
        self.assertEqual(env_a.requests, env_b.requests)
        self.assert_finite_report(agent)
        # Reports may include elapsed runtime, so compare their episode-sized
        # campaign and observation collections through the public decisions.
        self.assertTrue(first_report)
        self.assertEqual(len(env_a.pilot_history), len(env_b.pilot_history))

    def test_profile_and_tariff_row_order_does_not_change_decisions(self):
        ordered = PublicEnvironment()
        shuffled = PublicEnvironment(profile=ordered.customer_profile.sample(frac=1, random_state=9))
        shuffled.tariffs = shuffled.tariffs.sample(frac=1, random_state=5)
        first = self.make_agent().act(ordered)
        second = self.make_agent().act(shuffled)
        self.assertEqual(first, second)
        self.assertEqual(ordered.requests, shuffled.requests)

    def test_negative_pilots_use_explicit_free_fallback(self):
        env = PublicEnvironment(winner="no_profitable_offer")
        agent = self.make_agent()
        plan = agent.act(env)
        self.assert_plan_valid(plan, env)
        self.assertTrue(all(item["channel"] == "push" for item in plan))
        self.assert_finite_report(agent)
        self.assertTrue(agent.last_report.get("warnings"),
                        "Negative-evidence fallback must disclose its uncertainty")

    def test_unavailable_or_nonfinite_pilots_do_not_break_delivery(self):
        for malformed in (RuntimeError, ValueError, "nan", "inf", "missing_ratio"):
            with self.subTest(malformed=malformed):
                env = PublicEnvironment(malformed=malformed)
                agent = self.make_agent()
                plan = agent.act(env)
                self.assert_plan_valid(plan, env)
                self.assert_finite_report(agent)
                self.assertTrue(agent.last_report.get("warnings"))

    def test_no_pilots_left_still_produces_valid_final_plan(self):
        env = PublicEnvironment(pilots_left=0)
        agent = self.make_agent()
        self.assert_plan_valid(agent.act(env), env)
        self.assertEqual(env.requests, [])
        self.assert_finite_report(agent)

    def test_homogeneous_audience_over_campaign_cap_remains_eligible(self):
        profile = make_profile(2000)
        profile["arpu_segment"] = "HIGH"
        profile["data_segment"] = "HEAVY"
        profile["call_segment"] = "HIGH"
        env = PublicEnvironment(profile=profile)
        agent = self.make_agent()
        plan = agent.act(env)
        self.assert_plan_valid(plan, env)
        self.assertGreater(len(env.pilot_history), 0)
        self.assertEqual(len(plan), 1)
        self.assertEqual(agent.last_report["resources"]["final_contacts"], 5000)
        self.assert_finite_report(agent)

    def test_large_cells_do_not_reserve_away_every_pilot(self):
        for n_per_segment, contacts in ((6000, 15000), (2000, 1750), (60, 11), (60, 15)):
            with self.subTest(customers=n_per_segment * 3, contacts=contacts):
                profile = make_profile(n_per_segment)
                profile["arpu_segment"] = "HIGH"
                profile["data_segment"] = "HEAVY"
                profile["call_segment"] = "HIGH"
                env = PublicEnvironment(profile=profile, contacts=contacts)
                agent = self.make_agent()
                plan = agent.act(env)
                self.assertGreater(len(env.pilot_history), 0,
                                   "A capped final campaign must leave room for affordable pilots")
                self.assert_plan_valid(plan, env)
                self.assertGreater(env.remaining_contacts, 0)
                self.assert_finite_report(agent)

    def test_small_paid_budget_preserves_minimum_pilot_and_final_contact(self):
        for budget, contacts, pilot_possible in ((50, 15000, True), (44, 11, True), (43, 11, False)):
            with self.subTest(budget=budget, contacts=contacts):
                env = PublicEnvironment(profile=make_profile(60), budget=budget, contacts=contacts)
                env.channels = {"sms": env.channels["sms"]}
                agent = self.make_agent()
                plan = agent.act(env)
                self.assert_plan_valid(plan, env)
                self.assertEqual(bool(env.pilot_history), pilot_possible)
                if pilot_possible:
                    self.assertEqual(len(env.pilot_history), 1)
                    self.assertEqual(env.pilot_history[0]["n_customers"], 10)
                    self.assertEqual(env.pilot_history[0]["cost"], 40)
                self.assertGreaterEqual(env.remaining_budget, 4)
                self.assertGreaterEqual(agent.last_report["resources"]["final_contacts"], 1)
                self.assertGreaterEqual(agent.last_report["resources"]["projected_remaining_budget"], 0)
                self.assert_finite_report(agent)

    def optimizer_state(self):
        profile = make_profile(60)
        arm = Arm({"filter_current_tariff": "source", "filter_arpu_segment": "HIGH"},
                  "offer_a", "sms", 60, 60 * 7200.0)
        arm.observe(0.9, 100)
        return State(profile, set(make_tariffs().tariff_plan_code), CHANNELS,
                     100000, 15000, arms=[arm])

    def test_expired_deadline_uses_bounded_fallback_without_variant_search(self):
        state = self.optimizer_state()
        # Many pending hypotheses must not cause a full search after expiry.
        state.arms *= 200
        with patch("campaign_agents.optimizer.time.monotonic", return_value=2.0), \
             patch("campaign_agents.optimizer.variants", side_effect=AssertionError("Expired search resumed")):
            records = Optimizer().plan(state, 100000, 15000, deadline=1.0)
        self.assertEqual(len(records), 1)
        self.assertGreater(records[0]["expected_contacts"], 0)
        self.assertEqual(records[0]["channel"], "push")
        validate_strategy(pd.DataFrame(records), make_tariffs())
        self.assertTrue(any("лимиту времени" in warning for warning in state.warnings))

    def test_expiry_inside_optimizer_sweep_stops_further_option_scans(self):
        state = self.optimizer_state()
        arm = state.arms[0]
        indexes = np.flatnonzero(state.profile.arpu_segment.eq("HIGH"))
        options = [(arm.campaign(), indexes, arm)] * 200
        now = [0.0]
        evaluated = []

        def expire_after_first_option(*args):
            evaluated.append(1)
            now[0] = 2.0
            return effective_indexes(*args)

        with patch("campaign_agents.optimizer.time.monotonic", side_effect=lambda: now[0]), \
             patch("campaign_agents.optimizer.variants", return_value=options), \
             patch("campaign_agents.optimizer.effective_indexes", side_effect=expire_after_first_option):
            records = Optimizer().plan(state, 100000, 15000, deadline=1.0)
        self.assertEqual(len(evaluated), 1,
                         "Time expiry must be checked inside the sweep, not just between sweeps")
        self.assertEqual(len(records), 1)
        validate_strategy(pd.DataFrame(records), make_tariffs())

    def test_expired_deadline_keeps_valid_base_segment_without_refinements(self):
        state = self.optimizer_state()
        state.profile[["data_segment", "call_segment"]] = None
        with patch("campaign_agents.optimizer.time.monotonic", return_value=2.0):
            records = Optimizer().plan(state, 100000, 15000, deadline=1.0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["expected_contacts"], 60)
        self.assertNotIn("filter_data_segment", records[0])
        self.assertNotIn("filter_call_segment", records[0])
        validate_strategy(pd.DataFrame(records), make_tariffs())

    def test_tiny_audience_does_not_overspend_to_satisfy_pilot_minimum(self):
        env = PublicEnvironment(profile=make_profile(1), budget=0, contacts=3)
        agent = self.make_agent()
        self.assert_plan_valid(agent.act(env), env)
        self.assertGreaterEqual(env.remaining_contacts, 1)
        self.assertEqual(env.remaining_budget, 0)
        self.assert_finite_report(agent)

    def test_exhausted_contacts_returns_explained_empty_plan(self):
        env = PublicEnvironment(contacts=0)
        agent = self.make_agent()
        self.assertEqual(agent.act(env), [])
        self.assertEqual(env.requests, [])
        self.assert_finite_report(agent)
        self.assertTrue(agent.last_report.get("warnings"))

    def test_empty_profile_returns_explained_empty_plan(self):
        env = PublicEnvironment(profile=make_profile(1).iloc[:0].copy())
        agent = self.make_agent()
        self.assertEqual(agent.act(env), [])
        self.assertEqual(env.requests, [])
        self.assert_finite_report(agent)
        self.assertTrue(agent.last_report.get("warnings"))


if __name__ == "__main__":
    unittest.main()
