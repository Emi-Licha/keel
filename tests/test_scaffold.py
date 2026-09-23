import sys
import tempfile
import unittest
import yaml
import shlex
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scaffold import generate


class ScaffoldTests(unittest.TestCase):
    def test_generated_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = generate("payments", "payments-team", Path(tmp) / "payments service")
            deployment = yaml.safe_load((target / "deploy/deployment.yaml").read_text())
            pod = deployment["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertEqual(pod["containers"][0]["image"], "keel/payments:v1")
            self.assertTrue((target / "service/app.py").is_file())
            command = (target / "README.md").read_text().split("`")[1]
            self.assertEqual(shlex.split(command)[-1], str(target))
            with self.assertRaises(FileExistsError):
                generate("payments", "payments-team", target)

    def test_reject_invalid_names_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ["../escape", "UPPERCASE", "ab", "service-", "a" * 64]:
                with self.assertRaises(ValueError):
                    generate(name, "payments-team", Path(tmp) / "output")
            self.assertEqual(list(Path(tmp).iterdir()), [])
