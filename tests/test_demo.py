"""Regression tests for delivery safety and verification."""
import subprocess
import sys
import unittest
import json
import tempfile
import hashlib
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import demo


class VerificationTests(unittest.TestCase):
    def test_bootstrap_removes_obsolete_manifests_and_keeps_only_source_in_registry(self):
        real_run = demo.run

        def run(*args, **kwargs):
            if args[0] == "git":
                return "" if "push" in args else real_run(*args, **kwargs)
            return demo.CLUSTER if args[:3] == ("kind", "get", "clusters") else ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = demo.generate("payments", "payments-team", root / "source")
            vendor = root / "work/vendor"
            vendor.mkdir(parents=True)
            for filename in ("cilium-1.20.2.tgz", "argocd.yaml"):
                (vendor / filename).write_bytes(b"test")
            checksum = hashlib.sha256(b"test").hexdigest()
            demo.save(root / "dependencies.lock.json", {"cilium_chart_sha256": checksum, "argocd_sha256": checksum})
            dockerfile = root / "cluster/git/Dockerfile"
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text("FROM test\n")
            with patch.object(demo, "ROOT", root), patch.object(demo, "WORK", root / "work/keel"), \
                 patch.object(demo, "REPO", root / "work/keel/gitops"), patch.object(demo, "REPORT", None), \
                 patch.object(demo, "run", side_effect=run), patch.object(demo.shutil, "which", return_value="tool"), \
                 patch.object(demo, "verify_cluster_ownership"), patch.object(demo, "kube"), \
                 patch.object(demo, "apply_template"), patch.object(demo, "wait_sync"), \
                 patch.object(demo, "refresh_monitoring"), patch.object(demo, "verify_service"), \
                 patch.object(demo, "build_service_image", return_value="test-image"):
                demo.bootstrap_platform(source)
                (source / "deploy/service.yaml").unlink()
                demo.bootstrap_platform(source)
                self.assertFalse((demo.REPO / "payments/service.yaml").exists())
                self.assertEqual(demo.read_service_registry()["services"]["payments"], {"source": str(source.resolve())})
                self.assertEqual(demo.git("status", "--porcelain"), "")

    def test_cluster_ownership_rejects_missing_stale_and_invalid_records(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(demo, "WORK", Path(directory)), \
             patch.object(demo, "KUBECONFIG", Path(directory) / "kubeconfig"), \
             patch.object(demo, "run", return_value="current-node"):
            demo.KUBECONFIG.touch()
            marker = demo.WORK / "managed.json"
            with self.assertRaises(RuntimeError):
                demo.verify_cluster_ownership()
            for record in ("", "{}", json.dumps(dict(demo.cluster_identity(), node_id="old-node")),
                           json.dumps(dict(demo.cluster_identity(), workspace="/another/workspace"))):
                marker.write_text(record)
                with self.assertRaises((RuntimeError, ValueError)):
                    demo.verify_cluster_ownership()
            demo.save(marker, demo.cluster_identity())
            demo.verify_cluster_ownership()

    def test_up_reuses_registered_source_even_when_missing(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(demo, "WORK", Path(directory)), patch.object(demo, "REPORT", None), \
             patch.object(demo, "generate") as generate, \
             patch.object(demo, "bootstrap_platform") as bootstrap, \
             patch.object(sys, "argv", ["demo.py", "up", "--service", "payments"]):
            source = Path(directory) / "custom source"
            demo.save(demo.WORK / "state.json", {"services": {"payments": {"source": str(source)}}})
            demo.main()
            bootstrap.assert_called_once_with(source)
            generate.assert_not_called()

    def test_down_checks_identity_and_removes_record_only_after_success(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(demo, "WORK", Path(directory)), patch.object(demo, "REPORT", None), \
             patch.object(demo, "verify_cluster_ownership") as ownership, \
             patch.object(demo, "diagnostics"), patch.object(demo, "run") as run, \
             patch.object(sys, "argv", ["demo.py", "down", "--confirm-cluster", "keel-local"]):
            marker = demo.WORK / "managed.json"
            marker.touch()
            ownership.side_effect = RuntimeError("ownership mismatch")
            with self.assertRaises(SystemExit):
                demo.main()
            run.assert_not_called()
            ownership.side_effect = None
            run.side_effect = RuntimeError("deletion failed")
            with self.assertRaises(SystemExit):
                demo.main()
            self.assertTrue(marker.exists())
            run.side_effect = None
            demo.main()
            self.assertFalse(marker.exists())

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
