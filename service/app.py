"""HTTP workload for exercising delivery, metrics and recovery."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest


class Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, fail=False, version="v1"):
        super().__init__(address, Handler)
        self.fail, self.version = fail, version
        self.metrics = CollectorRegistry()
        self.requests = Counter("http_requests_total", "Application requests", ["route", "status"], registry=self.metrics)
        self.duration = Histogram("http_request_duration_seconds", "Application handler duration", registry=self.metrics)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(json.dumps({"event": "request", "message": fmt % args}), flush=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            return self.respond(200, generate_latest(self.server.metrics), CONTENT_TYPE_LATEST)
        if path in {"/healthz", "/readyz"}:
            return self.respond(200, b'{"status":"ok"}')
        route = "/" if path == "/" else "unmatched"
        status = (500 if self.server.fail else 200) if path == "/" else 404
        with self.server.duration.time():
            self.server.requests.labels(route, str(status)).inc()
            self.respond(status, json.dumps({"version": self.server.version,
                                            "status": "ok" if status == 200 else "error"}).encode())

    def respond(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    Service(("0.0.0.0", int(os.getenv("PORT", "8080"))),
            fail=os.getenv("FAIL_REQUESTS") == "true",
            version=os.getenv("APP_VERSION", "v1")).serve_forever()
