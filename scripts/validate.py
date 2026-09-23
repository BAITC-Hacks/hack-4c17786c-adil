"""Reproducible end-to-end checks against the unchanged public local evaluator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from agent import Agent
from local_eval import evaluate_agent
from make_submission import build_submission
from setup_case import ARCHIVE, ARCHIVE_SHA256, restore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    os.chdir(ROOT)
    restore()
    originals = ["environment.py", "mock_environment.py", "scoring_core.py", "local_eval.py", "make_submission.py", "agent_template.py", "PARTICIPANT_GUIDE.md"]
    with zipfile.ZipFile(ARCHIVE) as archive:
        for filename in originals:
            if (ROOT / filename).read_bytes() != archive.read(filename):
                raise AssertionError(f"Participant source was modified: {filename}")
    results = []
    for seed in [42, *range(args.runs)]:
        agent = Agent()
        started = time.monotonic()
        result = evaluate_agent(agent, seed=seed, verbose=False)
        duration = time.monotonic() - started
        assert result is not None, "No evaluator result"
        assert 1 <= len(agent.last_report["campaigns"]) <= 10
        assert 1 <= result["n_pilots"] <= 20
        assert result["total_contacts"] <= 15000 and result["total_cost"] <= 100000
        assert all(row["expected_contacts"] <= 5000 for row in agent.last_report["campaigns"])
        assert duration < 300, "Runtime exceeds stricter participant-template limit"
        results.append({"seed": seed, "net_arpu_gain": result["net_arpu_gain"],
                        "total_cost": result["total_cost"], "total_contacts": result["total_contacts"],
                        "pilots": result["n_pilots"], "final_campaigns": len(agent.last_report["campaigns"]),
                        "status": result["status"], "runtime_seconds": duration})
        print(f"seed={seed:2d} net={result['net_arpu_gain']:,.2f} campaigns={len(agent.last_report['campaigns'])} pilots={result['n_pilots']} time={duration:.2f}s")
    first, second = build_submission(Agent()), build_submission(Agent())
    pd.testing.assert_frame_equal(first, second)
    saved = pd.read_csv(ROOT / "submission.csv")
    pd.testing.assert_frame_equal(saved.fillna(""), first.fillna(""), check_dtype=False)
    canonical = first.to_csv(index=False, lineterminator="\n").encode("utf-8")
    gains = [row["net_arpu_gain"] for row in results[1:]]
    report = {
        "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
        "participant_archive_sha256": ARCHIVE_SHA256, "participant_sources_unchanged": originals,
        "submission_reproducible": True, "submission_sha256_lf": hashlib.sha256(canonical).hexdigest(),
        "single_run": results[0], "stability_runs": results[1:],
        "summary": {"runs": len(gains), "positive_runs": sum(value > 0 for value in gains),
                    "minimum_net": min(gains), "median_net": statistics.median(gains), "maximum_net": max(gains)},
        "scope": "Synthetic local effects only; these scores do not predict the hidden judging result.",
    }
    destination = ROOT / "artifacts" / "validation.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print("Submission reproduced; participant sources unchanged; all resource checks passed.")


if __name__ == "__main__":
    main()
