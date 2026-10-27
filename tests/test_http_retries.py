import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import service_update_report
from service_update_report import UpdateReportGenerator, create_http_session


class FlakyHandler(BaseHTTPRequestHandler):
    """Serves a scripted sequence of failures per path before answering normally."""

    plans: dict[str, list[str]] = {}
    hits: dict[str, int] = {}

    def do_GET(self):
        self.hits[self.path] = self.hits.get(self.path, 0) + 1
        plan = self.plans.get(self.path, [])
        action = plan.pop(0) if plan else "ok"
        if action == "drop":
            # Close the socket without writing a status line, as GitHub did in the failed run.
            self.close_connection = True
            self.connection.close()
            return
        status = {"ok": 200, "404": 404, "503": 503}[action]
        body = b"content"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class HttpRetriesTest(unittest.TestCase):
    def setUp(self):
        FlakyHandler.plans = {}
        FlakyHandler.hits = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        patcher = mock.patch.object(service_update_report, "HTTP_RETRY_BACKOFF_FACTOR", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        return create_http_session().get(f"{self.base}{path}", timeout=5)

    def test_dropped_connection_is_retried(self):
        FlakyHandler.plans["/file"] = ["drop", "drop"]
        response = self.get("/file")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.hits["/file"], 3)

    def test_server_error_is_retried(self):
        FlakyHandler.plans["/file"] = ["503"]
        response = self.get("/file")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.hits["/file"], 2)

    def test_not_found_is_returned_without_retry(self):
        FlakyHandler.plans["/file"] = ["404"]
        response = self.get("/file")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(FlakyHandler.hits["/file"], 1)

    def test_persistent_server_error_returns_last_response(self):
        attempts = service_update_report.HTTP_RETRY_ATTEMPTS + 1
        FlakyHandler.plans["/file"] = ["503"] * attempts
        response = self.get("/file")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(FlakyHandler.hits["/file"], attempts)

    def test_generator_uses_retrying_session(self):
        FlakyHandler.plans["/file"] = ["drop"]
        generator = UpdateReportGenerator("wodby")
        self.assertEqual(generator.session.get(f"{self.base}/file", timeout=5).status_code, 200)


if __name__ == "__main__":
    unittest.main()
