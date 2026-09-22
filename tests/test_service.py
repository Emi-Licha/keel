import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
from app import Service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = Service(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path):
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}{path}", timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.read().decode()

    def test_success_and_metrics(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertIn('http_requests_total{route="/",status="200"} 1', self.request("/metrics")[1])

    def test_failure_does_not_fail_process_health(self):
        self.server.fail = True
        self.assertEqual(self.request("/")[0], 500)
        self.assertEqual(self.request("/healthz")[0], 200)
        self.assertEqual(self.request("/readyz")[0], 200)
        self.assertIn('status="500"', self.request("/metrics")[1])
        self.server.fail = False
        self.assertEqual(self.request("/")[0], 200)

    def test_unknown_paths_have_bounded_metric_labels(self):
        for path in ["/customer/1", "/customer/2"]:
            self.assertEqual(self.request(path)[0], 404)
        metrics = self.request("/metrics")[1]
        self.assertIn('route="unmatched",status="404"} 2', metrics)
        self.assertNotIn("customer", metrics)

    def test_probes_do_not_inflate_application_successes(self):
        self.request("/healthz")
        self.request("/readyz")
        self.assertNotIn('status="200"', self.request("/metrics")[1])
