"""Run explicitly against the managed local cluster after bootstrap."""
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from demo import ROOT, apply_template, git, kube


class ClusterTests(unittest.TestCase):
    def test_network_boundaries(self):
        image = kube("-n", "keel-apps", "get", "deployment", "payments", "-o",
                     "jsonpath={.spec.template.spec.containers[0].image}")
        for namespace, expected in [("keel-system", "ALLOWED"), ("keel-untrusted", "DENIED")]:
            with self.subTest(namespace=namespace):
                name = "keel-probe-" + uuid.uuid4().hex[:8]
                try:
                    apply_template(ROOT / "tests/network-probe.yaml", namespace=namespace,
                                   pod_name=name, image=image)
                    kube("-n", namespace, "wait", "--for=jsonpath={.status.phase}=Succeeded",
                         "pod/" + name, "--timeout=45s", timeout=55)
                    self.assertEqual(kube("-n", namespace, "logs", name), expected)
                finally:
                    kube("-n", namespace, "delete", "pod", name, "--ignore-not-found", "--wait=false")

    def test_anonymous_git_push_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "access denied|service not enabled"):
            kube("-n", "keel-system", "exec", "deployment/keel-git", "--", "git", "-C",
                 "/repos/platform.git", "push", "git://127.0.0.1/platform.git", "main:main")

    def test_git_history_survives_pod_replacement(self):
        before = git("ls-remote", "origin", "refs/heads/main")
        self.assertTrue(before)
        kube("-n", "keel-system", "rollout", "restart", "deployment/keel-git")
        kube("-n", "keel-system", "rollout", "status", "deployment/keel-git",
             "--timeout=120s", timeout=140)
        self.assertEqual(git("ls-remote", "origin", "refs/heads/main"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
