"""Check a fix against a mock of the new API, and report exactly what was checked.

Prism generates the mock from the new spec. apimend puts a small recording proxy in front
of it, so every request your tests make is passed through to Prism and noted, together
with Prism's answer: 2xx means the request matched the new spec, 422 means it broke the
spec's rules, and 404 means the endpoint does not exist in the new spec.
"""

from __future__ import annotations

import http.client
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # only needed for type annotations, never imported at runtime
    if sys.version_info >= (3, 11):
        from typing import Self
    else:
        from typing_extensions import Self

# Headers that describe a single network hop and must not be copied between connections.
HOP_BY_HOP = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

VERIFIED = "verified"
TESTS_ONLY = "tests-only"
UNVERIFIED = "unverified"
FAILED = "failed"


class VerifyError(Exception):
    """Raised when verification cannot run because of a setup problem."""


@dataclass(frozen=True)
class RecordedRequest:
    """One request your tests sent to the mock, and how the mock answered."""

    method: str
    path: str
    status: int

    @property
    def matches_spec(self) -> bool:
        return 200 <= self.status < 400

    def describe(self) -> str:
        if self.matches_spec:
            verdict = "matches the new spec"
        elif self.status == 422:
            verdict = "does NOT match the new spec (invalid request)"
        elif self.status == 404:
            verdict = "endpoint is not in the new spec"
        else:
            verdict = f"rejected by the mock (HTTP {self.status})"
        return f"{self.method} {self.path} -> {self.status}: {verdict}"


@dataclass
class Report:
    """What verification checked, and what it found."""

    tests_passed: bool
    test_output: str
    mock_used: bool
    requests: list[RecordedRequest] = field(default_factory=list)
    remaining: int = 0  # affected lines still found after the fix

    @property
    def mismatches(self) -> list[RecordedRequest]:
        return [r for r in self.requests if not r.matches_spec]

    @property
    def verdict(self) -> str:
        if not self.tests_passed:
            return FAILED
        if not self.mock_used:
            return TESTS_ONLY
        if not self.requests:
            return UNVERIFIED
        if self.mismatches:
            return FAILED
        return VERIFIED


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def free_port() -> int:
    """Ask the operating system for a port nothing is using."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RecordingProxy:
    """A tiny HTTP proxy that forwards every request to `upstream_port` and records it."""

    def __init__(self, port: int, upstream_port: int) -> None:
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass  # keep the terminal quiet

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
                upstream = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=30)
                try:
                    upstream.request(self.command, self.path, body=body, headers=headers)
                    response = upstream.getresponse()
                    status, reply_headers, data = (
                        response.status,
                        response.getheaders(),
                        response.read(),
                    )
                except OSError:
                    status, reply_headers, data = (
                        502,
                        [],
                        b"apimend: the mock server is unavailable",
                    )
                finally:
                    upstream.close()
                proxy._record(RecordedRequest(self.command, self.path, status))
                self.send_response(status)
                for key, value in reply_headers:
                    if key.lower() not in HOP_BY_HOP:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record(self, request: RecordedRequest) -> None:
        with self._lock:
            self.requests.append(request)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class MockSession:
    """A Prism mock of `spec` behind a recording proxy listening on `port`."""

    def __init__(self, spec: str | Path, port: int, timeout: float = 60.0) -> None:
        self.spec, self.port, self.timeout = spec, port, timeout
        self._prism: subprocess.Popen | None = None
        self._proxy: RecordingProxy | None = None

    @property
    def requests(self) -> list[RecordedRequest]:
        return list(self._proxy.requests) if self._proxy else []

    def __enter__(self) -> Self:
        if shutil.which("prism") is None:
            raise VerifyError("Prism is not installed. Run 'apimend doctor' for help.")
        if port_in_use(self.port):
            raise VerifyError(
                f"port {self.port} is already in use. Stop that program or use --port."
            )
        internal = free_port()
        self._prism = subprocess.Popen(
            ["prism", "mock", str(self.spec), "--port", str(internal)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self._wait_for(internal)
            self._proxy = RecordingProxy(self.port, internal)
            self._proxy.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _wait_for(self, port: int) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._prism is not None and self._prism.poll() is not None:
                raise VerifyError(f"Prism stopped unexpectedly. Is {self.spec} a valid spec?")
            if port_in_use(port):
                return
            time.sleep(0.3)
        raise VerifyError(f"Prism did not start within {self.timeout:.0f} seconds")

    def __exit__(self, *exc_info: object) -> None:
        if self._proxy is not None:
            self._proxy.stop()
        if self._prism is not None:
            self._prism.terminate()
            try:
                self._prism.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._prism.kill()
                self._prism.wait()


def run_tests(command: str, port: int, timeout: float = 600.0) -> tuple[bool, str]:
    """Run the project's test command. Returns (passed, output)."""
    env = {**os.environ, "APIMEND_MOCK_URL": f"http://127.0.0.1:{port}"}
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VerifyError(f"test command not found: {exc.filename}") from exc
    except subprocess.TimeoutExpired:
        return False, f"tests did not finish within {timeout:.0f} seconds"
    return result.returncode == 0, result.stdout + result.stderr


def verify(
    test_command: str,
    spec: str | Path,
    port: int,
    use_mock: bool = True,
    recheck: Callable[[], int] | None = None,
) -> Report:
    """Run the tests (against a recorded mock of `spec`) and build a report."""
    if use_mock:
        with MockSession(spec, port) as mock:
            passed, output = run_tests(test_command, port)
            requests = mock.requests
    else:
        passed, output = run_tests(test_command, port)
        requests = []
    remaining = recheck() if recheck else 0
    return Report(passed, output, use_mock, requests, remaining)
