import http.server
import json
import logging
import random
import socketserver
import sys
import threading
import time

VERSION = "1.1.0"

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s demo-api %(message)s",
)
log = logging.getLogger("demo-api")

_lock = threading.Lock()
_requests_total = 0
_errors_total = 0
_duration_seconds_total = 0.0


def do_work():
    """Business logic. This is the function the 'broken commit' will change."""
    n = random.randint(1, 100)
    return {"result": n * 2, "version": VERSION}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        global _requests_total, _errors_total, _duration_seconds_total

        if self.path == "/healthz":
            self._respond(200, b"ok", "text/plain")
            return

        if self.path == "/metrics":
            with _lock:
                body = (
                    "# HELP demo_api_requests_total Total /work requests handled\n"
                    "# TYPE demo_api_requests_total counter\n"
                    f"demo_api_requests_total {_requests_total}\n"
                    "# HELP demo_api_errors_total Total /work requests that failed\n"
                    "# TYPE demo_api_errors_total counter\n"
                    f"demo_api_errors_total {_errors_total}\n"
                    "# HELP demo_api_request_duration_seconds_total Cumulative /work handling time\n"
                    "# TYPE demo_api_request_duration_seconds_total counter\n"
                    f"demo_api_request_duration_seconds_total {_duration_seconds_total:.6f}\n"
                )
            self._respond(200, body, "text/plain; version=0.0.4")
            return

        if self.path == "/work":
            with _lock:
                _requests_total += 1
            started = time.monotonic()
            try:
                result = do_work()
            except Exception:
                with _lock:
                    _errors_total += 1
                    _duration_seconds_total += time.monotonic() - started
                log.exception("path=/work status=500 unhandled exception in do_work")
                self._respond(500, json.dumps({"error": "internal server error"}))
                return
            with _lock:
                _duration_seconds_total += time.monotonic() - started
            log.info("path=/work status=200")
            self._respond(200, json.dumps(result))
            return

        self._respond(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    log.info(f"demo-api starting version={VERSION} port=8080")
    Server(("0.0.0.0", 8080), Handler).serve_forever()
