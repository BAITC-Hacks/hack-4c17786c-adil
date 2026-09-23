"""Regression checks for honest run evidence, explanations and CSV eligibility."""

import copy
import csv
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from webapp.evidence import build_explanations, build_validation
from webapp.worker import PublicEnvironmentProxy, execute_evaluations, validate_config


def fixture():
    campaign = {"campaign_name": "plan", "filter_current_tariff": "base",
                "filter_arpu_segment": "MID", "target_tariff": "offer", "channel": "sms"}
    pilot = {"pilot": "pilot_1", "filters": {"filter_current_tariff": "base", "filter_arpu_segment": "MID"},
             "target_tariff": "offer", "channel": "sms", "n_customers": 100,
             "cost": 400, "observed_lift_ratio": 0.10,
             "posterior_mean": 0.09, "standard_error": 0.07}
    report = {"pilots": [pilot], "public_pilot_history": [copy.deepcopy(pilot)],
              "campaigns": [{**campaign, "expected_contacts": 500, "expected_cost": 2000,
                             "expected_net_gain": 3000, "estimated_lift": 0.09,
                             "standard_error": 0.07, "reason": "Решение по наблюдениям."}]}
    score = {"net_arpu_gain": -123, "status": "FAIL", "n_pilots": 1, "n_campaigns": 2,
             "total_cost": 2400, "total_contacts": 600,
             "campaigns_detail": [{"name": "pilot_1", "channel": "sms", "cost": 400, "n_contacts": 100},
                                  {"name": "plan", "channel": "sms", "cost": 2000, "n_contacts": 500}]}
    return [campaign], report, score


class ResultValidationTests(unittest.TestCase):
    def test_negative_profit_is_not_a_contract_failure(self):
        campaigns, report, score = fixture()
        evidence = build_validation(campaigns, score, report, 1.5, plan_validated=True)
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["hidden_judging"], "not_checked")
        self.assertEqual(score["status"], "FAIL")
        self.assertTrue(all(item["status"] == "passed" for item in evidence["checks"]))
        self.assertTrue(all(not isinstance(item["observed"], (dict, list)) for item in evidence["checks"]))
        score.update(status="PASS", net_arpu_gain=123)
        self.assertEqual(build_validation(campaigns, score, report, 1.5, plan_validated=True), evidence)

    def test_missing_evidence_never_becomes_a_pass(self):
        campaigns, _, _ = fixture()
        result = build_validation(campaigns, {}, {}, None)
        self.assertEqual(result["status"], "not_checked")
        checks = {item["id"]: item for item in result["checks"]}
        for name in ("plan_schema", "pilot_history", "budget", "contacts", "runtime"):
            self.assertEqual(checks[name]["status"], "not_checked", name)

    def test_actual_limit_violations_are_failed(self):
        changes = [
            ("final_campaign_count", lambda c, r, s: c.clear()),
            ("final_campaign_count", lambda c, r, s: c.extend(c * 10)),
            ("pilot_count", lambda c, r, s: s.update(n_pilots=0)),
            ("pilot_count", lambda c, r, s: s.update(n_pilots=21)),
            ("pilot_sizes", lambda c, r, s: r["public_pilot_history"][0].update(n_customers=9)),
            ("pilot_sizes", lambda c, r, s: r["public_pilot_history"][0].update(n_customers=201)),
            ("budget", lambda c, r, s: s.update(total_cost=100001)),
            ("contacts", lambda c, r, s: s.update(total_contacts=15001)),
            ("final_campaign_contacts", lambda c, r, s: s["campaigns_detail"][1].update(n_contacts=0)),
            ("final_campaign_contacts", lambda c, r, s: s["campaigns_detail"][1].update(n_contacts=5001)),
            ("accounting", lambda c, r, s: s["campaigns_detail"][1].update(cost=1999)),
            ("scored_plan", lambda c, r, s: s["campaigns_detail"].pop()),
            ("pilot_history", lambda c, r, s: r["public_pilot_history"].clear()),
        ]
        for identifier, mutate in changes:
            with self.subTest(check=identifier):
                campaigns, report, score = fixture()
                mutate(campaigns, report, score)
                result = build_validation(campaigns, score, report, 1, plan_validated=True)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(next(item for item in result["checks"] if item["id"] == identifier)["status"], "failed")

    def test_runtime_uses_stricter_observed_limit_and_invalid_numbers_stay_unknown(self):
        campaigns, report, score = fixture()
        for value, expected in [(299.9, "passed"), (300, "failed"), (float("nan"), "not_checked"), (True, "not_checked")]:
            with self.subTest(runtime=value):
                result = build_validation(campaigns, score, report, value, plan_validated=True)
                self.assertEqual(next(item for item in result["checks"] if item["id"] == "runtime")["status"], expected)

    def test_same_counts_do_not_hide_replaced_campaigns_or_changed_pilot_accounting(self):
        mutations = [
            lambda s: s["campaigns_detail"][1].update(name="unrelated_campaign"),
            lambda s: s["campaigns_detail"][1].update(channel="push"),
            lambda s: s["campaigns_detail"][0].update(name="another_pilot"),
            lambda s: s["campaigns_detail"][0].update(channel="call"),
            lambda s: (s["campaigns_detail"][0].update(n_contacts=201),
                       s["campaigns_detail"][1].update(n_contacts=399)),
            lambda s: (s["campaigns_detail"][0].update(cost=500),
                       s["campaigns_detail"][1].update(cost=1900)),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                campaigns, report, score = fixture()
                mutate(score)
                evidence = build_validation(campaigns, score, report, 1, plan_validated=True)
                checks = {item["id"]: item["status"] for item in evidence["checks"]}
                self.assertEqual(checks["accounting"], "passed")
                self.assertEqual(checks["scored_plan"], "failed")
                self.assertEqual(evidence["status"], "failed")

    def test_missing_public_pilot_identity_is_unknown_and_optional_final_name_remains_optional(self):
        campaigns, report, score = fixture()
        campaigns[0].pop("campaign_name")
        score["campaigns_detail"][1]["name"] = None
        self.assertEqual(build_validation(campaigns, score, report, 1, plan_validated=True)["status"], "passed")
        report["public_pilot_history"][0].pop("pilot")
        evidence = build_validation(campaigns, score, report, 1, plan_validated=True)
        self.assertEqual(next(item for item in evidence["checks"] if item["id"] == "scored_plan")["status"], "not_checked")


class ExplanationTests(unittest.TestCase):
    def test_exact_pilots_are_linked_without_claiming_unique_customers_or_scorer_effect(self):
        campaigns, report, _ = fixture()
        report["pilots"].append({**report["pilots"][0], "n_customers": 200,
                                  "posterior_mean": 0.08, "observed_lift_ratio": 0.07})
        result = build_explanations(campaigns, report)[0]
        self.assertEqual(result["evidence_kind"], "pilot_observations")
        self.assertFalse(result["broader_segment"])
        self.assertEqual(result["pilot_evidence"]["pilot_indexes"], [1, 2])
        self.assertEqual(result["pilot_evidence"]["sample_contacts"], 300)
        self.assertEqual(result["pilot_evidence"]["posterior_mean"], 0.08)
        self.assertEqual(result["forecast"]["expected_net_gain"], 3000)
        self.assertNotIn("net_arpu_gain", result)

    def test_subgroup_transfer_is_explicit_and_unrelated_pilots_are_not_evidence(self):
        campaigns, report, _ = fixture()
        campaigns[0]["filter_data_segment"] = "HEAVY"
        self.assertTrue(build_explanations(campaigns, report)[0]["broader_segment"])
        campaigns[0]["filter_current_tariff"] = "another"
        result = build_explanations(campaigns, report)[0]
        self.assertEqual(result["evidence_kind"], "no_matching_pilot")
        self.assertIsNone(result["pilot_evidence"])
        self.assertIsNone(result["broader_segment"])
        self.assertEqual(result["observed_alternatives"], [])

    def test_alternatives_require_same_pilot_cell_and_one_changed_decision(self):
        campaigns, report, _ = fixture()
        first = report["pilots"][0]
        report["pilots"] += [
            {**first, "channel": "push"},
            {**first, "target_tariff": "other"},
            {**first, "channel": "call", "target_tariff": "other"},
            {**first, "channel": "call", "filters": {"filter_current_tariff": "another"}},
        ]
        alternatives = build_explanations(campaigns, report)[0]["observed_alternatives"]
        self.assertEqual([(item["kind"], item["target_tariff"], item["channel"]) for item in alternatives],
                         [("channel", "offer", "push"), ("tariff", "other", "sms")])

    def test_tariff_sets_and_more_specific_pilots_are_not_pooled(self):
        campaigns, report, _ = fixture()
        report["pilots"][0]["filters"]["filter_current_tariff"] = "base; another"
        self.assertTrue(build_explanations(campaigns, report)[0]["broader_segment"])
        report["pilots"].append({**report["pilots"][0], "filters": {
            "filter_current_tariff": "base", "filter_arpu_segment": "MID"}})
        result = build_explanations(campaigns, report)[0]
        self.assertEqual(result["pilot_evidence"]["pilot_indexes"], [2])
        self.assertFalse(result["broader_segment"])
        campaigns[0].pop("filter_current_tariff")
        self.assertEqual(build_explanations(campaigns, report)[0]["evidence_kind"], "no_matching_pilot")


class WorkerEvidenceTests(unittest.TestCase):
    def run_fake(self, config, output, score_change=None, report_change=None,
                 clock=None, act_seconds=0, evaluator_seconds=0):
        campaigns, base_report, base_score = fixture()

        class Candidate:
            def __init__(self, seed, **kwargs):
                self.seed = seed
                self.last_report = copy.deepcopy(base_report)

            def act(self, env):
                if clock is not None:
                    clock[0] += act_seconds
                plan = [{**campaigns[0], "campaign_name": f"plan_{self.seed}"}]
                self.last_report["campaigns"][0]["campaign_name"] = plan[0]["campaign_name"]
                return plan

        def report(agent, *args):
            value = copy.deepcopy(agent.last_report)
            if report_change:
                report_change(value)
            return value

        def evaluate(wrapper, seed, **kwargs):
            wrapper.act(SimpleNamespace(tariffs=pd.DataFrame({"tariff_plan_code": ["base", "offer"]})))
            if clock is not None:
                clock[0] += evaluator_seconds
            score = copy.deepcopy(base_score)
            score["net_arpu_gain"] = 100 if seed == 41 else -123
            score["campaigns_detail"][1]["name"] = f"plan_{seed}"
            if score_change:
                score_change(score)
            return score

        with (patch("agent.Agent", Candidate), patch("local_eval.evaluate_agent", side_effect=evaluate),
              patch("demo.build_report", side_effect=report), patch("demo.render_dashboard", return_value="report")):
            return execute_evaluations(config, output, lambda event: None)

    def test_batch_validation_and_submission_belong_to_their_own_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = self.run_fake(validate_config({"mode": "batch", "seed": 41, "runs": 2}), output)
            self.assertEqual(result["selected_seed"], 41)
            self.assertEqual(result["validation"], result["trials"][0]["validation"])
            self.assertTrue(result["submission"]["eligible"])
            self.assertEqual(result["submission"]["seed"], 42)
            self.assertEqual(result["submission"]["validation"], result["trials"][1]["validation"])
            self.assertEqual(result["submission"]["reproduction"], "not_checked_in_this_run")
            for filename, seed in [("campaign_plan.csv", 41), ("submission.csv", 42)]:
                rows = list(csv.DictReader(io.StringIO((output / filename).read_text(encoding="utf-8"))))
                self.assertEqual(rows[0]["campaign_name"], f"plan_{seed}")

    def test_custom_settings_cannot_be_labelled_standard_submission(self):
        for config in ({"seed": 7}, {"max_pilots": 4}, {"time_limit_seconds": 60}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                result = self.run_fake(validate_config(config), Path(directory))
                self.assertFalse(result["submission"]["eligible"])
                self.assertIsNone(result["submission"]["validation"])
                self.assertFalse((Path(directory) / "submission.csv").exists())
                self.assertTrue((Path(directory) / "campaign_plan.csv").exists())

    def test_runtime_limit_applies_to_agent_call_without_charging_evaluator_time(self):
        for act_seconds, evaluator_seconds, should_pass in ((1, 600, True), (301, 0, False)):
            with self.subTest(agent=act_seconds, evaluator=evaluator_seconds), tempfile.TemporaryDirectory() as directory:
                clock = [0]
                with patch("webapp.worker.time.perf_counter", side_effect=lambda: clock[0]):
                    if should_pass:
                        result = self.run_fake(validate_config({}), Path(directory), clock=clock,
                                               act_seconds=act_seconds, evaluator_seconds=evaluator_seconds)
                        trial = result["trials"][0]
                        self.assertEqual(trial["agent_runtime_seconds"], 1)
                        self.assertEqual(trial["runtime_seconds"], 601)
                        self.assertTrue(result["submission"]["eligible"])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "Agent.act"):
                            self.run_fake(validate_config({}), Path(directory), clock=clock,
                                          act_seconds=act_seconds, evaluator_seconds=evaluator_seconds)
                        self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failed_or_missing_evidence_prevents_successful_exports(self):
        for kwargs in ({"score_change": lambda value: value.update(total_cost=100001)},
                       {"report_change": lambda value: value.pop("public_pilot_history")}):
            with self.subTest(kwargs=list(kwargs)), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "Экспорт отменён"):
                    self.run_fake(validate_config({}), Path(directory), **kwargs)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_live_pilot_event_contains_only_supplied_public_filters(self):
        events = []
        env = SimpleNamespace(remaining_budget=900, remaining_contacts=100,
                              run_pilot=lambda **kwargs: {"n_customers": 10, "cost": 40})
        proxy = PublicEnvironmentProxy(env, events.append, 1, 1, 20)
        proxy.run_pilot(target_tariff="offer", channel="sms", n_customers=10,
                        filter_current_tariff="base", filter_arpu_segment="MID", filter_data_segment=None)
        self.assertEqual(events[0]["filters"], {"filter_current_tariff": "base", "filter_arpu_segment": "MID"})
        self.assertEqual(events[0]["target_tariff"], "offer")
        self.assertEqual(events[0]["channel"], "sms")


if __name__ == "__main__":
    unittest.main()
