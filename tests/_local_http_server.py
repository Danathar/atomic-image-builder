"""Loopback HTTP helpers for tests that need urllib.request to run its real
connect/read/error-parsing code, instead of a mocked urlopen.

Not collected as a test module (name doesn't start with ``test``), but
importable by files under tests/ once ``unittest discover -s tests`` puts
this directory on sys.path.
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import threading
from collections.abc import Iterator


class _FixedResponseHandler(http.server.BaseHTTPRequestHandler):
    status: int = 200
    body: bytes = b""

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass  # keep test output quiet


@contextlib.contextmanager
def local_http_server(*, status: int, body: bytes) -> Iterator[str]:
    """Serve a single fixed response on 127.0.0.1 for the duration of the block."""
    handler = type("_Handler", (_FixedResponseHandler,), {"status": status, "body": body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def closed_port_url() -> str:
    """A loopback URL nothing is listening on, to force a real connection-refused error."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/"
