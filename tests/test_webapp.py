"""Local dashboard boundary tests; workers are harmless temporary processes.

No test starts the campaign evaluator or the real test-mode worker, which would
recursively discover this module. Each HTTP server uses an ephemeral local port.
"""

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from webapp.server import Manager, make_server, validate_config


VALID_CONFIG = {
    "mode": "single", "seed": 42, "max_pilots": 20,
    "time_limit_seconds": 60, "runs": 1,
}

# -c receives but deliberately ignores the appended --output-dir/--config args.
# A shell is never needed, and cancellation can be tested before this exits.
SLEEPING_WORKER = [sys.executable, "-c", "import time; time.sleep(30)"]


class ConfigurationTests(unittest.TestCase):
    def test_supported_modes_accept_only_bounded_values(self):
        for mode in ("single", "batch", "tests"):
            with self.subTest(mode=mode):
                config = dict(VALID_CONFIG, mode=mode, runs=2 if mode == "batch" else 1)
                self.assertEqual(validate_config(config), config)

    def test_invalid_values_and_command_fields_are_rejected(self):
        invalid_changes = [
            {"mode": "shell"}, {"mode": []}, {"mode": {}},
            {"seed": -1}, {"seed": 2147483628},
            {"seed": True}, {"seed": "42; echo injected"},
            {"max_pilots": 0}, {"max_pilots": 21}, {"max_pilots": 1.5},
            {"time_limit_seconds": 9}, {"time_limit_seconds": 241},
            {"time_limit_seconds": float("nan")},
            {"time_limit_seconds": float("inf")},
            {"runs": 2}, {"mode": "batch", "runs": 1},
            {"mode": "batch", "runs": 21},
            {"command": "echo injected"}, {"output_dir": "../../outside"},
        ]
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    validate_config(dict(VALID_CONFIG, **changes))
        for payload in (None, [], "single", 42):
            with self.subTest(payload=payload):
                with self.assertRaises((TypeError, ValueError)):
                    validate_config(payload)


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="campaign-web-test-")
        self.root = Path(self.temp.name) / "project"
        (self.root / "web").mkdir(parents=True)
        (self.root / "web" / "index.html").write_text("SAFE STATIC INDEX", encoding="utf-8")
        (self.root / "secret.txt").write_text("PRIVATE_TEST_MARKER", encoding="utf-8")
        self.data_dir = Path(self.temp.name) / "runs"
        self.manager = Manager(root=self.root, data_dir=self.data_dir,
                               worker_command=SLEEPING_WORKER)
        self.server = make_server(self.manager, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.manager.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, raw_body=None, headers=None):
        request_headers = dict(headers or {})
        body = raw_body
        if payload is not None:
            body = json.dumps(payload)
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def create_run(self, config=None):
        status, _, body = self.request("POST", "/api/runs", payload=config or VALID_CONFIG)
        self.assertEqual(status, 202, body)
        run = json.loads(body)["run"]
        self.assertEqual(len(run["id"]), 32)
        self.assertEqual(uuid.UUID(hex=run["id"]).hex, run["id"])
        return run

    def wait_for_status(self, run_id, expected, timeout=5):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status, _, body = self.request("GET", f"/api/runs/{run_id}")
            self.assertEqual(status, 200, body)
            last = json.loads(body)["run"]
            if last["status"] in expected:
                return last
            time.sleep(0.02)
        self.fail(f"Run did not reach {expected}: {last}")

    def test_invalid_api_configuration_cannot_enqueue_work(self):
        invalid = [
            dict(VALID_CONFIG, mode=[]),
            dict(VALID_CONFIG, mode={}),
            dict(VALID_CONFIG, seed="42; echo injected"),
            dict(VALID_CONFIG, max_pilots=21),
            dict(VALID_CONFIG, mode="batch", runs=1),
            dict(VALID_CONFIG, command="echo injected"),
        ]
        for config in invalid:
            with self.subTest(config=config):
                status, _, body = self.request("POST", "/api/runs", payload=config)
                self.assertEqual(status, 400, body)
        status, _, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["runs"], [])

    def test_mutations_require_json_and_valid_json_syntax(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded"):
            with self.subTest(content_type=content_type):
                status, _, body = self.request(
                    "POST", "/api/runs", raw_body=json.dumps(VALID_CONFIG),
                    headers={"Content-Type": content_type},
                )
                self.assertEqual(status, 415, body)
        status, _, body = self.request(
            "POST", "/api/runs", raw_body="{broken json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, body)

    def test_all_actions_reject_non_object_or_unexpected_json(self):
        run = self.create_run()
        self.wait_for_status(run["id"], {"running"})
        for path in ("/api/data/prepare", f"/api/runs/{run['id']}/cancel"):
            for raw in ("null", "[]", '"text"', '{"unexpected":true}', "[" * 1100 + "]" * 1100):
                with self.subTest(path=path, raw=raw[:40]):
                    status, _, body = self.request("POST", path, raw_body=raw,
                                                   headers={"Content-Type": "application/json"})
                    self.assertEqual(status, 400, body)
        self.assertEqual(self.manager.get_run(run["id"])["status"], "running")

    def test_missing_data_still_returns_usable_overview(self):
        status, _, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 200, body)
        dataset = json.loads(body)["dataset"]
        self.assertFalse(dataset["ready"])
        self.assertIn("customer_profile.csv", dataset["error"])
        self.assertEqual(dataset["customers"], 0)

    def test_failed_initial_write_cannot_leave_an_unqueued_active_job(self):
        with mock.patch.object(self.manager, "_save", side_effect=PermissionError("test disk full")):
            status, _, body = self.request("POST", "/api/runs", payload=VALID_CONFIG)
            self.assertEqual(status, 500, body)
        self.assertEqual(self.manager.list_runs(), [])
        self.assertEqual(self.manager.pending.unfinished_tasks, 0)
        self.assertTrue(self.manager.thread.is_alive())
        self.create_run()

    def test_state_write_failure_does_not_kill_queue(self):
        save = self.manager._save
        failed = []

        def fail_first_start(job):
            if job["status"] == "running" and not failed:
                failed.append(job["id"])
                raise PermissionError("test read-only history")
            save(job)

        with mock.patch.object(self.manager, "_save", side_effect=fail_first_start):
            run = self.create_run()
            failed_run = self.wait_for_status(run["id"], {"failed"})
            self.assertIn("test read-only history", failed_run["error"])
            next_run = self.create_run()
            self.wait_for_status(next_run["id"], {"running"})
        self.assertTrue(self.manager.thread.is_alive())

    def test_cancellation_stops_process_even_if_history_cannot_be_saved(self):
        run = self.create_run()
        self.wait_for_status(run["id"], {"running"})
        deadline = time.monotonic() + 3
        while run["id"] not in self.manager.processes and time.monotonic() < deadline:
            time.sleep(0.01)
        process = self.manager.processes[run["id"]]
        with mock.patch.object(self.manager, "_save", side_effect=PermissionError("test disk full")):
            status, _, body = self.request("POST", f"/api/runs/{run['id']}/cancel", payload={})
            self.assertEqual(status, 200, body)
            cancelled = json.loads(body)["run"]
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertIn("Не удалось сохранить", cancelled["error"])
        process.wait(timeout=3)
        self.assertIsNotNone(process.poll())

    def test_bad_result_does_not_break_detail_endpoint(self):
        run = self.create_run()
        result_path = self.data_dir / run["id"] / "result.json"
        for content in ("[]", '{"score":NaN}', '{"score":1e999}', "{broken"):
            with self.subTest(content=content):
                result_path.write_text(content, encoding="utf-8")
                status, _, body = self.request("GET", f"/api/runs/{run['id']}")
                self.assertEqual(status, 200, body)
                details = json.loads(body)["run"]
                self.assertIsNone(details["result"])
                self.assertIn("result_error", details)

    def test_foreign_origin_and_host_cannot_create_runs(self):
        for headers in (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Host": "evil.example"},
            {"Host": "127.0.0.1.evil.example"},
        ):
            with self.subTest(headers=headers):
                status, _, body = self.request("POST", "/api/runs", payload=VALID_CONFIG,
                                               headers=headers)
                self.assertEqual(status, 403, body)
        status, _, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["runs"], [])

    def test_same_origin_browser_can_create_list_and_read_a_run(self):
        status, _, body = self.request(
            "POST", "/api/runs", payload=VALID_CONFIG,
            headers={"Origin": f"http://127.0.0.1:{self.port}",
                     "Content-Type": "application/json; charset=UTF-8"},
        )
        self.assertEqual(status, 202, body)
        created = json.loads(body)["run"]
        status, _, body = self.request("GET", f"/api/runs/{created['id']}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["run"]["config"], VALID_CONFIG)
        status, _, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        self.assertEqual([run["id"] for run in json.loads(body)["runs"]], [created["id"]])

    def test_static_routes_cannot_escape_frontend_directory(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200, body)
        self.assertIn(b"SAFE STATIC INDEX", body)
        for path in ("/../secret.txt", "/%2e%2e/secret.txt",
                     "/..%2fsecret.txt", "/%2e%2e%5csecret.txt",
                     "/secret.txt", "/../runs/metadata.json"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertIn(status, (403, 404), body)
                self.assertNotIn(b"PRIVATE_TEST_MARKER", body)

    def test_run_artifact_endpoint_uses_filename_allowlist(self):
        run = self.create_run()
        run_dir = self.data_dir / run["id"]
        (run_dir / "report.json").write_text('{"artifact":"safe-report"}', encoding="utf-8")
        (run_dir / "private.txt").write_text("PRIVATE_TEST_MARKER", encoding="utf-8")
        status, headers, body = self.request("GET", f"/api/runs/{run['id']}/files/report.json")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["artifact"], "safe-report")
        self.assertIn("application/json", headers.get("Content-Type", ""))
        for filename in ("metadata.json", "private.txt", "../private.txt", "%2e%2e/metadata.json"):
            with self.subTest(filename=filename):
                status, _, body = self.request("GET", f"/api/runs/{run['id']}/files/{filename}")
                self.assertIn(status, (403, 404), body)
                self.assertNotIn(b"PRIVATE_TEST_MARKER", body)

    def test_running_fake_worker_can_be_cancelled(self):
        run = self.create_run()
        self.wait_for_status(run["id"], {"running"})
        status, _, body = self.request("POST", f"/api/runs/{run['id']}/cancel", payload={})
        self.assertEqual(status, 200, body)
        self.wait_for_status(run["id"], {"cancelled"})
        self.assertEqual(json.loads((self.data_dir / run["id"] / "metadata.json").read_text(
            encoding="utf-8"))["status"], "cancelled")


class RecoveryTests(unittest.TestCase):
    def test_corrupt_metadata_is_preserved_and_does_not_break_startup(self):
        with tempfile.TemporaryDirectory(prefix="campaign-corrupt-history-") as directory:
            root = Path(directory)
            data_dir = root / "runs"
            base = {"status": "completed", "config": VALID_CONFIG,
                    "created_at": "2026-09-23T00:00:00+00:00"}
            invalid = [None, [], "text", {}, dict(base, status=[]),
                       dict(base, created_at=None), dict(base, config=[]),
                       dict(base, progress=[]), dict(base, events=["broken"]),
                       dict(base, summary={"score": float("nan")})]
            originals = {}
            for value in invalid:
                run_id = uuid.uuid4().hex
                if isinstance(value, dict):
                    value["id"] = run_id
                run_dir = data_dir / run_id
                run_dir.mkdir(parents=True)
                path = run_dir / "metadata.json"
                originals[path] = json.dumps(value)
                path.write_text(originals[path], encoding="utf-8")
            manager = Manager(root=root, data_dir=data_dir, worker_command=SLEEPING_WORKER)
            try:
                self.assertEqual(manager.list_runs(), [])
                self.assertEqual(len(manager.history_warnings), len(invalid))
                self.assertEqual(len(manager.overview()["history_warnings"]), len(invalid))
                for path, original in originals.items():
                    self.assertEqual(path.read_text(encoding="utf-8"), original)
            finally:
                manager.close()

    def test_restart_marks_unfinished_jobs_interrupted(self):
        with tempfile.TemporaryDirectory(prefix="campaign-recovery-test-") as directory:
            root = Path(directory)
            data_dir = root / "runs"
            ids = []
            for status in ("running", "queued", "completed"):
                run_id = uuid.uuid4().hex
                ids.append((run_id, status))
                run_dir = data_dir / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "metadata.json").write_text(json.dumps({
                    "id": run_id, "status": status, "config": dict(VALID_CONFIG),
                    "created_at": "2026-09-23T00:00:00+00:00",
                    "updated_at": "2026-09-23T00:00:01+00:00",
                    "error": None,
                }), encoding="utf-8")
            manager = Manager(root=root, data_dir=data_dir, worker_command=SLEEPING_WORKER)
            try:
                for run_id, before in ids:
                    persisted = json.loads((data_dir / run_id / "metadata.json").read_text(encoding="utf-8"))
                    expected = "completed" if before == "completed" else "interrupted"
                    self.assertEqual(persisted["status"], expected)
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
