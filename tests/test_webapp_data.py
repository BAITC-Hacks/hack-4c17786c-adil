"""Regressions for damaged local inputs and actionable startup errors."""
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import app
from setup_case import ARCHIVE, DATA_FILES, restore
from webapp.server import Manager


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="campaign-dataset-test-")
        self.root = Path(self.temp.name)
        self.archive = self.root / "vendor" / ARCHIVE.name
        self.archive.parent.mkdir()
        shutil.copyfile(ARCHIVE, self.archive)
        restore(self.root, self.archive)
        self.manager = Manager(root=self.root)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_missing_csv_is_reported_and_prepare_restores_it(self):
        initial = self.manager.dataset()
        self.assertTrue(initial["ready"])
        self.assertTrue(all(row["valid"] and row["error"] is None for row in initial["files"]))
        self.assertGreater(initial["customers"], 0)
        path = self.root / "data" / "traffic.csv"
        original = path.read_bytes()
        path.unlink()
        missing = self.manager.dataset()
        self.assertFalse(missing["ready"])
        self.assertIn("data/traffic.csv", missing["error"])
        self.assertTrue(all(not row["valid"] for row in missing["files"]))
        self.assertTrue(all(row["error"] for row in missing["files"]))
        self.assertTrue(self.manager.prepare_data()["ready"])
        self.assertEqual(path.read_bytes(), original)

    def test_all_seven_csv_files_are_checked_before_claiming_ready(self):
        for name in DATA_FILES:
            with self.subTest(file=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(b"")
                broken = self.manager.dataset()
                self.assertFalse(broken["ready"])
                self.assertIn(name, broken["error"])
                self.assertEqual(broken["customers"], 0)
                for row in broken["files"]:
                    self.assertEqual(row["valid"], row["name"] != name)
                    self.assertEqual(row["error"] is None, row["name"] != name)
                    self.assertTrue(row["exists"])
                path.write_bytes(original)
        self.assertTrue(self.manager.dataset()["ready"])

    def test_invalid_profile_values_do_not_reach_json_metrics(self):
        path = self.root / "customer_profile.csv"
        header = "predicted_arpu,arpu_segment,data_segment,call_segment\n"
        for content in (header, header + "inf,LOW,LITE,LOW\n",
                        header + "not-a-number,LOW,LITE,LOW\n", '"unterminated'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                result = self.manager.overview()
                self.assertFalse(result["dataset"]["ready"])
                self.assertEqual(result["dataset"]["baseline_arpu"], 0.0)
                self.assertIn("customer_profile.csv", result["dataset"]["error"])

    def test_prepare_preserves_modified_files_and_explains_recovery(self):
        path = self.root / "data" / "change_tariff.csv"
        path.write_text("modified by user", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "не были перезаписаны"):
            self.manager.prepare_data()
        self.assertEqual(path.read_text(encoding="utf-8"), "modified by user")
        self.assertFalse(self.manager.dataset()["ready"])

    def test_missing_or_damaged_archive_is_not_marked_ready(self):
        self.archive.write_bytes(b"damaged archive")
        broken = self.manager.dataset()
        self.assertFalse(broken["ready"])
        self.assertIn("Контрольная сумма", broken["error"])
        self.assertTrue(all(not row["valid"] and "Не проверен:" in row["error"] for row in broken["files"]))
        self.archive.unlink()
        missing = self.manager.dataset()
        self.assertFalse(missing["ready"])
        self.assertIn(ARCHIVE.name, missing["error"])


class StartupTests(unittest.TestCase):
    def test_unwritable_history_has_actionable_message(self):
        with (mock.patch.object(sys, "argv", ["app.py"]),
              mock.patch.object(app, "urlopen", side_effect=OSError("offline")),
              mock.patch.object(app, "restore", return_value=([], [])),
              mock.patch.object(app, "Manager", side_effect=PermissionError("read-only")),
              mock.patch("builtins.print")):
            with self.assertRaisesRegex(SystemExit, "Проверьте права записи"):
                app.main()

    def test_other_service_json_does_not_crash_health_probe(self):
        with (mock.patch.object(sys, "argv", ["app.py"]),
              mock.patch.object(app, "urlopen", return_value=io.StringIO("[]")),
              mock.patch.object(app, "restore", return_value=([], [])),
              mock.patch.object(app, "Manager") as manager,
              mock.patch.object(app, "make_server", side_effect=OSError("address in use")),
              mock.patch("builtins.print")):
            manager.return_value.history_warnings = []
            with self.assertRaisesRegex(SystemExit, "Попробуйте другой порт"):
                app.main()
            manager.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
