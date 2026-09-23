"""Execute one local Studio job and stream newline-delimited JSON progress.

Only the public evaluator scores runs. The agent and progress adapter access
the documented environment API; no scoring/model internals are inspected.
Job outputs stay in the directory supplied by the HTTP job manager.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
import numbers
import os
from pathlib import Path
import statistics
import sys
import time
import unittest

from webapp.evidence import build_explanations, build_validation


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_COLUMNS = [
    "campaign_name", "filter_arpu_segment", "filter_data_segment",
    "filter_call_segment", "filter_current_tariff", "target_tariff", "channel",
]
PUBLIC_ENV_FIELDS = frozenset({
    "customer_profile", "tariffs", "channels", "total_budget",
    "max_total_contacts", "remaining_budget", "remaining_contacts",
    "pilots_left", "pilot_history",
})


def plain(value):
    """Convert scientific scalars to strict JSON; nonfinite numbers are null."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def validate_config(config):
    """Validate again in the worker, independent of the HTTP boundary."""
    if not isinstance(config, dict):
        raise ValueError("Параметры запуска должны быть JSON-объектом.")
    allowed = {"mode", "seed", "max_pilots", "time_limit_seconds", "runs"}
    if set(config) - allowed:
        raise ValueError("Неизвестные параметры запуска.")
    mode = config.get("mode", "single")
    if mode not in ("single", "batch", "tests"):
        raise ValueError("Режим должен быть single, batch или tests.")
    result = {
        "mode": mode,
        "seed": config.get("seed", 42),
        "max_pilots": config.get("max_pilots", 20),
        "time_limit_seconds": config.get("time_limit_seconds", 240),
        "runs": config.get("runs", 10 if mode == "batch" else 1),
    }
    limits = {
        "seed": (0, 2147483627), "max_pilots": (1, 20),
        "time_limit_seconds": (10, 240),
        "runs": (2, 20) if mode == "batch" else (1, 1),
    }
    for key, (minimum, maximum) in limits.items():
        value = result[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key}: ожидается целое число от {minimum} до {maximum}.")
    return result


class EventEmitter:
    def __init__(self, stream):
        self.stream = stream

    def __call__(self, event):
        self.stream.write(json.dumps(plain(event), ensure_ascii=False, allow_nan=False) + "\n")
        self.stream.flush()


class PublicEnvironmentProxy:
    """Forward only documented public fields and publish pilot observations."""

    def __init__(self, env, emit, trial_index, trial_count, max_pilots):
        self._env = env
        self._emit = emit
        self._trial_index = trial_index
        self._trial_count = trial_count
        self._max_pilots = max_pilots
        self._pilot_calls = 0

    def __getattr__(self, name):
        if name in PUBLIC_ENV_FIELDS:
            return getattr(self._env, name)
        raise AttributeError(name)

    def run_pilot(self, *args, **kwargs):
        self._pilot_calls += 1
        observation = {}
        failure = None
        try:
            observation = self._env.run_pilot(*args, **kwargs)
            return observation
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            observation = observation if isinstance(observation, dict) else {}
            event = {
                "type": "pilot", "trial_index": self._trial_index,
                "trial_count": self._trial_count, "pilot_index": self._pilot_calls,
                "max_pilots": self._max_pilots,
                "n_customers": observation.get("n_customers"),
                "cost": observation.get("cost"),
                "target_tariff": observation.get("target_tariff", kwargs.get("target_tariff", args[0] if args else None)),
                "channel": observation.get("channel", kwargs.get("channel", args[1] if len(args) > 1 else None)),
                "observed_lift_ratio": observation.get("observed_lift_ratio"),
                "remaining_budget": self._env.remaining_budget,
                "remaining_contacts": self._env.remaining_contacts,
                "filters": {key: kwargs[key] for key in CAMPAIGN_COLUMNS
                            if key.startswith("filter_") and kwargs.get(key) is not None},
            }
            if failure:
                event["error"] = failure
            self._emit(event)


class ReportingAgent:
    def __init__(self, agent, config, seed, trial_index, emit):
        self.agent = agent
        self.config = config
        self.seed = seed
        self.trial_index = trial_index
        self.emit = emit
        self.campaigns = []
        self.report = {}
        self.error = None
        self.plan_validated = False
        self.agent_runtime_seconds = None

    def act(self, env):
        from demo import build_report

        proxy = PublicEnvironmentProxy(env, self.emit, self.trial_index,
                                       self.config["runs"], self.config["max_pilots"])
        started = time.perf_counter()
        try:
            self.emit({"type": "stage", "message": "Агент изучает аудиторию и проверяет гипотезы.",
                       "trial_index": self.trial_index, "trial_count": self.config["runs"]})
            act_started = time.perf_counter()
            self.campaigns = self.agent.act(proxy) or []
            self.agent_runtime_seconds = time.perf_counter() - act_started
            validate_plan(self.campaigns, proxy.tariffs)
            self.plan_validated = True
            self.report = build_report(self.agent, proxy, self.campaigns,
                                       self.seed, time.perf_counter() - started)
            self.emit({"type": "stage", "message": "План готов. Локальный оценщик рассчитывает результат.",
                       "trial_index": self.trial_index, "trial_count": self.config["runs"]})
        except Exception as exc:
            # evaluate_agent catches agent errors; preserve them so the job does
            # not silently present a partial evaluation as a successful run.
            self.error = exc
            raise
        return self.campaigns


def validate_plan(campaigns, tariffs):
    """Reject a broken export before the evaluator silently sanitizes it.

    This adapter belongs to the local runner, not to the submitted agent. It
    uses the supplied public validator and never reads evaluator internals.
    """
    import pandas as pd
    from scoring_core import validate_strategy

    if not isinstance(campaigns, list) or not 1 <= len(campaigns) <= 10:
        raise ValueError("Агент должен вернуть список из 1–10 кампаний; отчёт не сформирован.")
    for index, campaign in enumerate(campaigns, 1):
        if not isinstance(campaign, dict):
            raise ValueError(f"Кампания {index}: ожидается словарь.")
        unknown = set(campaign) - set(CAMPAIGN_COLUMNS)
        if unknown:
            raise ValueError(f"Кампания {index}: поля вне контракта сдачи: {', '.join(sorted(map(str, unknown)))}.")
    # Include optional columns even for an intentionally unfiltered plan.
    frame = pd.DataFrame(campaigns).reindex(columns=CAMPAIGN_COLUMNS)
    try:
        validate_strategy(frame, tariffs)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"План агента не соответствует контракту сдачи: {exc}") from exc


def write_text(output_dir, name, content):
    destination = output_dir / name
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="")
    temporary.replace(destination)


def write_json(output_dir, name, value):
    write_text(output_dir, name, json.dumps(plain(value), ensure_ascii=False,
                                          indent=2, allow_nan=False) + "\n")


def csv_text(rows, columns):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(plain(rows))
    return buffer.getvalue()


def execute_evaluations(config, output_dir, emit):
    from agent import Agent
    from demo import render_dashboard
    from local_eval import evaluate_agent

    started = time.perf_counter()
    trials = []
    selected_report = None
    selected_trial = None
    selected_index = None
    for offset in range(config["runs"]):
        seed = config["seed"] + offset
        trial_index = offset + 1
        emit({"type": "stage", "message": f"Подготовка локального прогона {trial_index} из {config['runs']} (seed {seed}).",
              "trial_index": trial_index, "trial_count": config["runs"], "pilot_index": 0})
        agent = Agent(seed=seed, max_pilots=config["max_pilots"],
                      time_limit_seconds=config["time_limit_seconds"])
        wrapper = ReportingAgent(agent, config, seed, trial_index, emit)
        trial_started = time.perf_counter()
        score = evaluate_agent(wrapper, seed=seed, verbose=False)
        if wrapper.error is not None:
            raise RuntimeError(f"Агент завершился с ошибкой: {wrapper.error}") from wrapper.error
        if not isinstance(score, dict) or not isinstance(score.get("net_arpu_gain"), numbers.Real) or not math.isfinite(float(score["net_arpu_gain"])):
            raise RuntimeError("Локальный оценщик не вернул конечный числовой результат.")
        if (not isinstance(score.get("n_pilots"), numbers.Integral)
                or score.get("n_campaigns") != score["n_pilots"] + len(wrapper.campaigns)):
            raise RuntimeError("Число оценённых кампаний не совпадает с пилотами и финальным планом. Экспорт отменён.")
        campaigns = [{column: item.get(column) for column in CAMPAIGN_COLUMNS}
                     for item in wrapper.campaigns]
        runtime_seconds = time.perf_counter() - trial_started
        validation = build_validation(campaigns, score, wrapper.report, wrapper.agent_runtime_seconds,
                                      plan_validated=wrapper.plan_validated)
        if validation["status"] != "passed":
            failed_checks = "; ".join(
                f"{item['label']}: {'нарушение' if item['status'] == 'failed' else 'нет подтверждения'}"
                for item in validation["checks"] if item["status"] != "passed")
            raise RuntimeError(f"Проверка результата не завершилась успешно. Экспорт отменён. {failed_checks}")
        explanations = build_explanations(campaigns, wrapper.report)
        trial = plain({"seed": seed, "score": score, "report": agent.last_report,
                       "campaigns": campaigns,
                       "runtime_seconds": runtime_seconds,
                       "agent_runtime_seconds": wrapper.agent_runtime_seconds,
                       "validation": validation, "explanations": explanations})
        trials.append(trial)
        if selected_trial is None or trial["score"]["net_arpu_gain"] > selected_trial["score"]["net_arpu_gain"]:
            selected_trial = trial
            selected_report = wrapper.report
            selected_index = offset
        emit({"type": "trial_complete", "trial_index": trial_index,
              "trial_count": config["runs"], "seed": seed,
              "score": {key: trial["score"].get(key) for key in
                        ("net_arpu_gain", "gross_arpu_lift", "total_cost", "n_pilots")},
              "net_arpu_gain": trial["score"]["net_arpu_gain"],
              "runtime_seconds": trial["runtime_seconds"]})
    gains = [item["score"]["net_arpu_gain"] for item in trials]
    summary = {
        "trials": len(trials), "positive_runs": sum(value > 0 for value in gains),
        "mean_net": statistics.mean(gains), "median_net": statistics.median(gains),
        "min_net": min(gains), "max_net": max(gains),
        "best_seed": selected_trial["seed"], "best_net": max(gains),
        "total_pilots": sum(item["score"].get("n_pilots", 0) for item in trials),
        "total_runtime_seconds": time.perf_counter() - started,
    }
    artifacts = ["result.json", "report.json", "report.html", "campaign_plan.csv"]
    write_json(output_dir, "report.json", selected_report)
    # The existing standalone template links its original JSON filename.
    dashboard = render_dashboard(selected_report).replace('href="demo_report.json"', 'href="report.json"')
    write_text(output_dir, "report.html", dashboard)
    write_text(output_dir, "campaign_plan.csv", csv_text(selected_trial["campaigns"], CAMPAIGN_COLUMNS))
    submission_trial = next((trial for trial in trials if trial["seed"] == 42), None)
    if config["max_pilots"] != 20 or config["time_limit_seconds"] != 240:
        submission_trial = None
    export_notes = {
        "campaign_plan.csv": f"План выбранного эксперимента, seed {selected_trial['seed']}. Параметры запуска указаны в result.json.",
    }
    if submission_trial is not None:
        write_text(output_dir, "submission.csv", csv_text(submission_trial["campaigns"], CAMPAIGN_COLUMNS))
        artifacts.append("submission.csv")
        submission_note = "submission.csv содержит план со стандартными параметрами сдачи: seed 42, 20 пилотов и лимит 240 секунд. В серии он может отличаться от плана лучшего прогона. Повторная генерация проверяется отдельно."
        export_notes["submission.csv"] = submission_note
    else:
        submission_note = "Для submission.csv запустите seed 42 с 20 пилотами и лимитом 240 секунд. Текущий campaign_plan.csv — план эксперимента."
    submission = {
        "eligible": submission_trial is not None,
        "seed": 42 if submission_trial is not None else None,
        "reason": ("План seed 42 прошёл локальные проверки контракта; использованы стандартные параметры make_submission.py. "
                   "Повторная генерация в рамках этого запуска не выполнялась."
                   if submission_trial is not None else submission_note),
        "validation": submission_trial["validation"] if submission_trial is not None else None,
        "reproduction": "not_checked_in_this_run",
    }
    if config["mode"] == "batch":
        comparison_columns = ["seed", "net_arpu_gain", "gross_arpu_lift", "total_cost",
                              "n_pilots", "final_campaigns", "runtime_seconds", "selected"]
        comparison = []
        for item in trials:
            row = {key: item["score"].get(key) for key in comparison_columns}
            row.update(seed=item["seed"], final_campaigns=len(item["campaigns"]),
                       runtime_seconds=item["runtime_seconds"],
                       selected=item["seed"] == selected_trial["seed"])
            comparison.append(row)
        write_text(output_dir, "comparison.csv", csv_text(comparison, comparison_columns))
        artifacts.append("comparison.csv")
    result = {
        "mode": config["mode"], "config": config, "summary": summary,
        "selected_seed": selected_trial["seed"], "selected_trial_index": selected_index,
        "selection_rule": "highest_local_mock_net_gain" if config["mode"] == "batch" else "single_run",
        "score_kind": "local_mock_evaluation",
        "note": "Результат получен на учебной модели. Прогнозы агента и локальная оценка — разные показатели; локальная оценка не предсказывает балл судейства.",
        "submission_note": submission_note,
        "submission_seed": 42 if submission_trial is not None else None,
        "submission": submission,
        "export_notes": export_notes,
        "score": selected_trial["score"], "campaigns": selected_trial["campaigns"],
        "report": selected_report, "trials": trials, "artifacts": artifacts,
        "validation": selected_trial["validation"],
        "explanations": selected_trial["explanations"],
    }
    write_json(output_dir, "result.json", result)
    return result


def execute_tests(config, output_dir, emit):
    started = time.perf_counter()
    emit({"type": "stage", "message": "Проверка агента и локального приложения: unittest discover.",
          "trial_index": 1, "trial_count": 1})
    log = io.StringIO()
    # Run in this process so terminating a job cannot leave a unittest child.
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        suite = unittest.TestLoader().discover(str(PROJECT_ROOT / "tests"), pattern="test*.py")
        outcome = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    successful = outcome.wasSuccessful() and outcome.testsRun > 0
    summary = {
        "trials": 0, "positive_runs": 0,
        "total_runtime_seconds": time.perf_counter() - started,
        "tests_run": outcome.testsRun, "tests_passed": successful,
        "failures": len(outcome.failures), "errors": len(outcome.errors),
        "skipped": len(outcome.skipped),
    }
    write_text(output_dir, "tests.txt", log.getvalue())
    result = {"mode": "tests", "config": config, "summary": summary,
              "tests_passed": successful, "exit_code": 0 if successful else 1,
              "selected_seed": None, "trials": [],
              "artifacts": ["result.json", "tests.txt"]}
    write_json(output_dir, "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Фоновый процесс локального Campaign Studio.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    emit = EventEmitter(sys.stdout)
    try:
        config = validate_config(json.loads(args.config))
        output_dir = args.output_dir.resolve(strict=True)
        if not output_dir.is_dir():
            raise ValueError("Каталог результатов не существует.")
        # All evaluator input paths are project-relative. Preserve stdout for
        # events and capture legacy diagnostic printing away from the protocol.
        os.chdir(PROJECT_ROOT)
        diagnostics = io.StringIO()
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            result = (execute_tests(config, output_dir, emit) if config["mode"] == "tests"
                      else execute_evaluations(config, output_dir, emit))
        emit({"type": "complete", "result": result["summary"],
              "artifacts": result["artifacts"]})
        return 0
    except Exception as exc:
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
