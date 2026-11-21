import json
import shlex
import shutil
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from apimend.cli import main
from apimend.verify import (
    FAILED,
    PARTIAL,
    TESTS_ONLY,
    UNVERIFIED,
    VERIFIED,
    RecordedRequest,
    RecordingProxy,
    Report,
    free_port,
)

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
PYTHON = shlex.quote(sys.executable)
needs_prism = pytest.mark.skipif(shutil.which("prism") is None, reason="Prism not installed")


class FakeUpstream(BaseHTTPRequestHandler):
    """Stands in for Prism: accepts POST /orders with a body, rejects everything else."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ok = self.path == "/orders" and json.loads(body).get("pizza_size")
        reply = b'{"id": "ord_1"}' if ok else b'{"error": "invalid"}'
        self.send_response(201 if ok else 422)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def do_GET(self):
        # A header "folded" over two lines: once parsed, its value contains a line break.
        self.send_response(200)
        self.send_header("X-Folded", "first\r\n second")
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def proxy():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    recording = RecordingProxy(free_port(), upstream.server_address[1])
    recording.start()
    yield recording
    recording.stop()
    upstream.shutdown()
    upstream.server_close()


def post(proxy, path, payload):
    port = proxy._server.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_proxy_forwards_requests_and_records_the_answers(proxy):
    assert post(proxy, "/orders", {"pizza_size": "large"}) == (201, b'{"id": "ord_1"}')
    assert post(proxy, "/orders", {"size": "large"})[0] == 422
    assert proxy.requests == [
        RecordedRequest("POST", "/orders", 201),
        RecordedRequest("POST", "/orders", 422),
    ]


def test_recorded_requests_explain_what_the_mock_said():
    assert "matches the new spec" in RecordedRequest("POST", "/orders", 201).describe()
    assert "does NOT match" in RecordedRequest("POST", "/orders", 422).describe()
    assert "not in the new spec" in RecordedRequest("GET", "/old", 404).describe()
    assert "HTTP 401" in RecordedRequest("GET", "/me", 401).describe()


@pytest.mark.parametrize(
    ("report", "verdict"),
    [
        (Report(False, "", True, [RecordedRequest("POST", "/orders", 201)]), FAILED),
        (Report(True, "", False), TESTS_ONLY),
        (Report(True, "", True, []), UNVERIFIED),
        (Report(True, "", True, [RecordedRequest("POST", "/orders", 422)]), FAILED),
        (Report(True, "", True, [RecordedRequest("POST", "/orders", 201)]), VERIFIED),
    ],
)
def test_verdict_only_claims_what_the_evidence_shows(report, verdict):
    assert report.verdict == verdict


ORDERS = ("POST", "/orders")
PLACED = RecordedRequest("POST", "/orders", 201)


def test_leftover_affected_lines_prevent_a_full_verification():
    report = Report(True, "", True, [PLACED], remaining=1, affected=[ORDERS], changed=[ORDERS])
    assert report.verdict == PARTIAL
    assert report.untested_changed == []  # the fix itself was tested


def test_an_affected_endpoint_the_tests_never_called_prevents_a_full_verification():
    menu = RecordedRequest("GET", "/menu", 200)
    report = Report(True, "", True, [menu], affected=[ORDERS], changed=[ORDERS])
    assert report.verdict == PARTIAL
    assert report.untested_affected == [ORDERS]
    assert report.untested_changed == [ORDERS]


def test_full_verification_needs_every_affected_endpoint_called():
    report = Report(True, "", True, [PLACED], affected=[ORDERS], changed=[ORDERS])
    assert report.verdict == VERIFIED


@pytest.mark.parametrize(
    ("request_", "called"),
    [
        (RecordedRequest("GET", "/orders/42", 200), True),  # path parameter
        (RecordedRequest("GET", "/orders/42?expand=items", 200), True),  # query string
        (RecordedRequest("GET", "/v2/orders/42", 200), True),  # base path in front
        (RecordedRequest("DELETE", "/orders/42", 204), False),  # another HTTP method
        (RecordedRequest("GET", "/orders/42/items", 200), False),  # another endpoint
        (RecordedRequest("GET", "/archive-orders/42", 200), False),  # not a path boundary
    ],
)
def test_requests_are_matched_to_the_endpoints_they_call(request_, called):
    report = Report(True, "", True, [request_], affected=[("GET", "/orders/{id}")])
    assert (report.untested_affected == []) is called


@pytest.fixture
def lab(tmp_path):
    """A private copy of the Pizza API lab, so tests can change it safely."""
    return Path(shutil.copytree(LAB, tmp_path / "pizza-api"))


def fix_args(lab, *extra):
    return ["fix", str(lab / "v1.yaml"), str(lab / "v2.yaml"), str(lab / "app"), *extra]


@needs_prism
def test_tests_that_never_call_the_api_do_not_count_as_verified(lab, capsys):
    before = (lab / "app" / "pizza_client.py").read_bytes()
    port = str(free_port())
    assert main(fix_args(lab, "--port", port, "--test", f"{PYTHON} -c 'pass'")) == 1
    assert "UNVERIFIED: your tests passed but never called the API" in capsys.readouterr().out
    assert (lab / "app" / "pizza_client.py").read_bytes() == before


@needs_prism
def test_allow_unverified_keeps_the_fix_and_says_so(lab, capsys):
    port = str(free_port())
    args = fix_args(lab, "--port", port, "--test", f"{PYTHON} -c 'pass'", "--allow-unverified")
    assert main(args) == 0
    assert "kept anyway because --allow-unverified was used" in capsys.readouterr().out
    assert '"pizza_size": size' in (lab / "app" / "pizza_client.py").read_text()


@needs_prism
def test_passing_tests_that_send_invalid_requests_are_caught(lab, tmp_path, capsys):
    before = (lab / "app" / "pizza_client.py").read_bytes()
    careless = tmp_path / "careless_test.py"
    careless.write_text(
        "import json, os, urllib.error, urllib.request\n"
        "url = os.environ['APIMEND_MOCK_URL'] + '/orders'\n"
        "body = json.dumps({'pizza': 'margherita'}).encode()\n"
        "request = urllib.request.Request(url, data=body, method='POST',\n"
        "                                 headers={'Content-Type': 'application/json'})\n"
        "try:\n"
        "    urllib.request.urlopen(request)\n"
        "except urllib.error.HTTPError:\n"
        "    pass  # a careless test that swallows the error\n"
    )
    port = str(free_port())
    command = f"{PYTHON} {shlex.quote(str(careless))}"
    assert main(fix_args(lab, "--port", port, "--test", command)) == 1
    out = capsys.readouterr().out
    assert "POST /orders -> 422: does NOT match the new spec" in out
    assert "NOT VERIFIED: some requests did not match the new spec" in out
    assert (lab / "app" / "pizza_client.py").read_bytes() == before


def test_proxy_never_passes_line_breaks_in_headers(proxy):
    """A line break in a header value could inject extra headers (HTTP response splitting)."""
    port = proxy._server.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(b"GET /folded HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        raw = b""
        while chunk := conn.recv(4096):
            raw += chunk
    head = raw.split(b"\r\n\r\n", 1)[0].decode()
    lines = head.split("\r\n")
    assert lines[0] == "HTTP/1.1 200 OK"
    assert "X-Folded: first second" in lines
    assert not any(line[:1] in (" ", "\t") for line in lines)  # nothing continues a header
    names = [line.split(":", 1)[0].lower() for line in lines[1:]]
    assert len(names) == len(set(names))  # no header is sent twice


@needs_prism
def test_a_partial_repair_is_kept_but_not_called_verified(lab, capsys, monkeypatch):
    # A second request apimend is less sure about (the HTTP method is unknown), so it
    # is left for review instead of being changed.
    (lab / "app" / "extra.py").write_text(
        'def reorder(size):\n    return send("/orders", {"size": size})\n'
    )
    port = free_port()
    monkeypatch.setenv("PIZZA_API_URL", f"http://127.0.0.1:{port}")  # the lab's API address
    command = f"{PYTHON} -m pytest -q -p no:cacheprovider {shlex.quote(str(lab / 'app'))}"
    assert main(fix_args(lab, "--port", str(port), "--test", command)) == 1
    out = capsys.readouterr().out
    assert "Re-scan: 1 affected line(s) remain with the fix applied, for you to review" in out
    assert "PARTIALLY VERIFIED" in out and "the fix has been kept" in out
    assert "VERIFIED against the new spec" not in out
    assert '"pizza_size": size' in (lab / "app" / "pizza_client.py").read_text()


@needs_prism
def test_a_fix_the_tests_never_exercised_is_undone(lab, capsys, monkeypatch):
    before = (lab / "app" / "pizza_client.py").read_bytes()
    (lab / "app" / "test_pizza_client.py").write_text(
        "import pizza_client\n\n\ndef test_menu():\n    assert pizza_client.get_menu()\n"
    )
    port = free_port()
    monkeypatch.setenv("PIZZA_API_URL", f"http://127.0.0.1:{port}")  # the lab's API address
    command = f"{PYTHON} -m pytest -q -p no:cacheprovider {shlex.quote(str(lab / 'app'))}"
    assert main(fix_args(lab, "--port", str(port), "--test", command)) == 1
    out = capsys.readouterr().out
    assert "[missing] Coverage: your tests never called POST /orders" in out
    assert "your tests never called POST /orders, which the fix changes" in out
    assert "The fix was undone." in out
    assert (lab / "app" / "pizza_client.py").read_bytes() == before
