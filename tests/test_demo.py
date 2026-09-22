"""Regression coverage for checks that must survive optimized Python."""
import subprocess
import sys
import unittest
import json
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import demo


class VerificationTests(unittest.TestCase):
    def test_failed_push_still_reverts_the_local_failure_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(demo, "WORK", Path(directory)):
                report = demo.Report("exercise")
            pushes = 0

            def git(*args):
                nonlocal pushes
                if args[0] == "push":
                    pushes += 1
                    if pushes == 1:
                        raise RuntimeError("push unavailable")
                return "" if args[0] == "status" else "baseline"

            with patch.object(demo, "REPORT", report), patch.object(demo, "git", side_effect=git) as commands, \
                 patch.object(demo, "read_service_registry", return_value={"services": {"payments": {"source": directory}}}), \
                 patch.object(demo, "verify_service"), patch.object(demo, "wait_sync"), \
                 patch.object(demo, "build_service_image", return_value="failure-image"), \
                 patch.object(demo, "set_deployment_image"), patch.object(demo, "drive_alert"), \
                 patch.object(demo, "commit", return_value="failure-commit"):
                with self.assertRaisesRegex(RuntimeError, "push unavailable") as failure:
                    demo.exercise_recovery("payments")
                report.finish(failure.exception)
            self.assertTrue(any("revert" in call.args for call in commands.call_args_list))
            result = json.loads(report.path.read_text())
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["recovered"])
            self.assertEqual(pushes, 2)

    def test_reports_are_unique_and_record_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(demo, "WORK", Path(directory)):
            first, second = demo.Report("up"), demo.Report("up")
            first.finish(RuntimeError("injected failure"))
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(json.loads(first.path.read_text())["status"], "failed")
            self.assertEqual(json.loads(second.path.read_text())["status"], "running")

    def test_optimized_python_rejects_failed_response(self):
        source = '''
import sys
from contextlib import nullcontext
from unittest.mock import patch
sys.path.insert(0, "scripts")
import demo
with patch.object(demo, "git", return_value="test"), \\
     patch.object(demo, "wait_sync"), \\
     patch.object(demo, "port_forward", return_value=nullcontext("http://test")), \\
     patch.object(demo, "request", return_value=(500, "broken")):
    demo.verify_service("payments")
'''
        result = subprocess.run(
            [sys.executable, "-O", "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected HTTP 200, received 500", result.stderr)
        self.assertNotIn("PASS:", result.stdout)
