import json
import shlex
import shutil
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
