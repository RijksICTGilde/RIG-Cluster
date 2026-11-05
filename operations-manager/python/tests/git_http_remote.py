"""A git remote over HTTP that demands Basic auth, served by ``git http-backend``.

A remote on disk answers every request without asking who is there, so a git command that
reaches the remote without credentials passes in a test and fails in production. This
remote refuses such a request, the way Forgejo and GitHub do.
"""

import base64
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

USERNAME = "opi"
PASSWORD = "s3cret-token"

GIT = shutil.which("git") or "/usr/bin/git"


def http_backend() -> str | None:
    exec_path = subprocess.run([GIT, "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
    backend = os.path.join(exec_path, "git-http-backend")
    return backend if os.path.exists(backend) else None


def _handler(project_root: Path, backend: str, requests: list[tuple[str, str, bool]]) -> type[BaseHTTPRequestHandler]:
    expected = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def _serve(self) -> None:
            authorized = self.headers.get("Authorization") == expected
            path, _, query = self.path.partition("?")
            requests.append((self.command, self.path, authorized))
            if not authorized:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            env = {
                "PATH": os.environ.get("PATH", ""),
                "GIT_PROJECT_ROOT": str(project_root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_USER": USERNAME,
                "REMOTE_ADDR": "127.0.0.1",
            }
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            if self.headers.get("Git-Protocol"):
                env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
            done = subprocess.run([backend], input=body, env=env, capture_output=True, check=False)
            head, _, payload = done.stdout.partition(b"\r\n\r\n")
            status = 200
            headers: list[tuple[str, str]] = []
            for line in head.decode("latin-1").split("\r\n"):
                if not line:
                    continue
                name, _, value = line.partition(":")
                if name.lower() == "status":
                    status = int(value.strip().split()[0])
                else:
                    headers.append((name, value.strip()))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _serve
        do_POST = _serve

    return Handler


@contextmanager
def serve(project_root: Path) -> Iterator[tuple[str, list[tuple[str, str, bool]]]]:
    """Serve the bare repos under ``project_root``; yields the base URL and the request log.

    Each logged request is (method, path, authorized), so a test can assert that no
    request ever went out without credentials.
    """
    backend = http_backend()
    if backend is None:
        raise RuntimeError("git-http-backend not found next to git")
    requests: list[tuple[str, str, bool]] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(project_root, backend, requests))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()
