"""Exercise real HTTP jobs and exports without adding runs to user history."""
import csv
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setup_case import restore
from webapp.server import Manager, make_server


def main():
    restore()
    observations = []
    with tempfile.TemporaryDirectory(prefix="campaign-studio-validation-") as temporary:
        manager = Manager(root=ROOT, data_dir=temporary)
        server = make_server(manager, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        def request(method, path, payload=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            headers = {"Origin": f"http://127.0.0.1:{port}"}
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload)
            try:
                connection.request(method, path, body=data, headers=headers)
                response = connection.getresponse()
                body = response.read()
                assert 200 <= response.status < 300, (response.status, body[:300])
                return body
            finally:
                connection.close()

        def json_request(method, path, payload=None):
            return json.loads(request(method, path, payload))

        try:
            overview = json_request("GET", "/api/overview")
            assert overview["dataset"]["ready"] and overview["dataset"]["customers"] > 0
            for asset in ("/", "/app.js", "/styles.css"):
                assert request("GET", asset)
            configurations = [
                {"mode": "single", "seed": 7, "max_pilots": 4, "time_limit_seconds": 60, "runs": 1},
                {"mode": "batch", "seed": 41, "max_pilots": 20, "time_limit_seconds": 240, "runs": 2},
                {"mode": "tests", "seed": 42, "max_pilots": 20, "time_limit_seconds": 240, "runs": 1},
            ]
            for config in configurations:
                started = time.monotonic()
                job = json_request("POST", "/api/runs", config)["run"]
                while job["status"] in {"queued", "running"}:
                    if time.monotonic() - started > 120:
                        raise AssertionError("HTTP job exceeded smoke-test deadline")
                    time.sleep(0.1)
                    job = json_request("GET", "/api/runs/" + job["id"])["run"]
                assert job["status"] == "completed", job.get("error")
                artifacts = {item["name"]: request("GET", item["url"]) for item in job["artifacts"]}
                if config["mode"] != "tests":
                    assert "campaign_plan.csv" in artifacts
                    assert len(job["result"]["trials"]) == config["runs"]
                    assert any(event["type"] == "pilot" for event in job["events"])
                    for trial in job["result"]["trials"]:
                        assert 1 <= len(trial["campaigns"]) <= 10
                        assert trial["score"]["total_cost"] <= 100000
                        assert trial["score"]["total_contacts"] <= 15000
                    if config["mode"] == "single":
                        assert "submission.csv" not in artifacts, "Custom runs cannot claim official reproducibility"
                    else:
                        assert "comparison.csv" in artifacts and "submission.csv" in artifacts
                        actual = list(csv.DictReader(io.StringIO(artifacts["submission.csv"].decode("utf-8"))))
                        expected = list(csv.DictReader(io.StringIO((ROOT / "submission.csv").read_text(encoding="utf-8"))))
                        assert actual == expected, "Official export must reproduce default seed42, not selected best seed"
                else:
                    assert job["summary"]["tests_passed"] and job["summary"]["tests_run"] >= 21
                    assert "tests.txt" in artifacts
                observations.append({"mode": config["mode"], "config": config, "status": job["status"],
                                     "summary": job["summary"], "artifacts": sorted(artifacts),
                                     "runtime_seconds": time.monotonic() - started})
                print(f"HTTP {config['mode']}: PASS, artifacts={', '.join(sorted(artifacts))}")
            report = {"status": "PASS", "dataset_customers": overview["dataset"]["customers"],
                      "checks": ["static UI assets", "actual HTTP background runs", "live pilot events", "isolated exports", "official seed42 reproduction", "in-app test execution"],
                      "runs": observations}
            (ROOT / "artifacts" / "web_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print("Saved artifacts/web_validation.json")
        finally:
            server.shutdown()
            server.server_close()
            manager.close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
