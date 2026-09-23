"""A scored plan and its exported CSV must obey the same public contract."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from webapp.worker import ReportingAgent, execute_evaluations, validate_config


class RunnerContractTests(unittest.TestCase):
    def setUp(self):
        self.env = SimpleNamespace(tariffs=pd.DataFrame({"tariff_plan_code": ["base", "offer"]}))
        self.config = validate_config({})
        self.valid = {"target_tariff": "offer", "channel": "push", "filter_current_tariff": "base"}

    def wrapper(self, plan):
        return ReportingAgent(SimpleNamespace(act=lambda env: plan), self.config, 42, 1, lambda event: None)

    def test_invalid_raw_plans_fail_before_report_and_export(self):
        plans = [[], {}, ["campaign"], [self.valid] * 11,
                 [{**self.valid, "target_tariff": "unknown"}],
                 [{**self.valid, "channel": "unknown"}],
                 [{**self.valid, "filter_arpu_segment": "INVALID"}],
                 [{**self.valid, "filter_current_tariff": "unknown"}],
                 [{**self.valid, "explicit_ids": [1, 2]}]]
        for plan in plans:
            with self.subTest(plan=plan), patch("demo.build_report") as build:
                wrapper = self.wrapper(plan)
                with self.assertRaises(ValueError):
                    wrapper.act(self.env)
                self.assertIsInstance(wrapper.error, ValueError)
                build.assert_not_called()

    def test_valid_filtered_and_unfiltered_plans_reach_report_unchanged(self):
        for plan in ([self.valid], [{"target_tariff": "offer", "channel": "sms"}]):
            with self.subTest(plan=plan), patch("demo.build_report", return_value={}) as build:
                wrapper = self.wrapper(plan)
                self.assertEqual(wrapper.act(self.env), plan)
                self.assertIsNone(wrapper.error)
                self.assertEqual(build.call_args.args[2], plan)

    def test_evaluator_recovery_does_not_turn_contract_error_into_success(self):
        # The supplied evaluator catches agent errors and may score earlier
        # pilots. A finite partial score must not publish a broken final CSV.
        def partial_evaluation(wrapper, **kwargs):
            try:
                wrapper.act(self.env)
            except ValueError:
                pass
            return {"net_arpu_gain": 123, "n_pilots": 1}

        bad_agent = SimpleNamespace(act=lambda env: [{**self.valid, "target_tariff": "unknown"}])
        with tempfile.TemporaryDirectory() as directory:
            with patch("agent.Agent", return_value=bad_agent), patch("local_eval.evaluate_agent", side_effect=partial_evaluation):
                with self.assertRaisesRegex(RuntimeError, "контракту сдачи"):
                    execute_evaluations(self.config, Path(directory), lambda event: None)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_stage_reporting_failures_are_not_hidden_by_evaluator_recovery(self):
        for message in ("Агент изучает", "План готов"):
            with self.subTest(stage=message), tempfile.TemporaryDirectory() as directory:
                failure = RuntimeError("reporting callback failed")
                wrappers = []

                def emit(event):
                    if event.get("message", "").startswith(message):
                        raise failure

                def partial_evaluation(wrapper, **kwargs):
                    wrappers.append(wrapper)
                    try:
                        wrapper.act(self.env)
                    except RuntimeError:
                        pass
                    return {"net_arpu_gain": 123, "n_pilots": 1, "n_campaigns": 1}

                candidate = SimpleNamespace(act=lambda env: [self.valid])
                with (patch("agent.Agent", return_value=candidate),
                      patch("demo.build_report", return_value={}),
                      patch("local_eval.evaluate_agent", side_effect=partial_evaluation)):
                    with self.assertRaisesRegex(RuntimeError, "reporting callback failed"):
                        execute_evaluations(self.config, Path(directory), emit)
                self.assertIs(wrappers[0].error, failure)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_dropped_final_campaign_cannot_be_exported_with_partial_score(self):
        def partial_evaluation(wrapper, **kwargs):
            wrapper.act(self.env)
            return {"net_arpu_gain": 123, "n_pilots": 1, "n_campaigns": 1}

        candidate = SimpleNamespace(act=lambda env: [self.valid])
        with tempfile.TemporaryDirectory() as directory:
            with (patch("agent.Agent", return_value=candidate),
                  patch("demo.build_report", return_value={}),
                  patch("local_eval.evaluate_agent", side_effect=partial_evaluation)):
                with self.assertRaisesRegex(RuntimeError, "Экспорт отменён"):
                    execute_evaluations(self.config, Path(directory), lambda event: None)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
