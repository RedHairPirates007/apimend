"""Which calls are HTTP requests: only calls with evidence count (review finding P0-1).

A call is a request when it uses a known HTTP client, when it calls a function in the
same file that is proven to send one, or when the project declares the function.
Anything else, such as a logging call, must never be treated as a request.
"""

from pathlib import Path

import pytest

from apimend.callsites import analyse
from apimend.cli import main
from apimend.fix import plan_fixes
from apimend.scan import HIGH, LOW, NEW_REQUIRED_PROPERTY, REMOVED_PROPERTY, Change, find_usages

LAB = Path(__file__).resolve().parent.parent / "lab" / "pizza-api"
V1, V2 = str(LAB / "v1.yaml"), str(LAB / "v2.yaml")
SIZE_REMOVED = Change("POST", "/orders", REMOVED_PROPERTY, "size", "renamed")
SIZE_REQUIRED = Change("POST", "/orders", NEW_REQUIRED_PROPERTY, "pizza_size", "new name")


def lines(tmp_path, source, change=SIZE_REMOVED, **options):
    """Scan one file and return {line: confidence}."""
    (tmp_path / "client.py").write_text(source)
    return {u.line: u.confidence for u in find_usages(tmp_path, change, **options)}


def requests_in(source):
    return [(c.line, c.method, c.paths) for c in analyse(source).calls]


# --- the reported false positive ---------------------------------------------


LOGGING = """\
import logging
logger = logging.getLogger(__name__)
def note(size):
    logger.info("POST", "/orders", {"size": 42})
    logging.warning("POST", "/orders", {"size": size})
    print("POST", "/orders", {"size": size})
"""


def test_logging_calls_are_never_requests(tmp_path):
    assert requests_in(LOGGING) == []
    assert lines(tmp_path, LOGGING) == {}
    assert lines(tmp_path, LOGGING, SIZE_REQUIRED) == {}


def test_logging_calls_are_never_changed(tmp_path):
    (tmp_path / "client.py").write_text(LOGGING)
    assert plan_fixes([SIZE_REMOVED, SIZE_REQUIRED], tmp_path, min_confidence="low") == []


def test_reported_logging_case_through_the_command_line(tmp_path, capsys):
    (tmp_path / "app.py").write_text(LOGGING)
    assert main(["scan", V1, V2, str(tmp_path)]) == 0
    assert "Your code does not appear to be affected." in capsys.readouterr().out


# --- unknown functions are not requests, but are never silently ignored ------


UNKNOWN = 'def order(size):\n    track("POST", "/orders", {"size": size})\n'


def test_an_unknown_function_is_not_a_request(tmp_path):
    assert requests_in(UNKNOWN) == []
    assert lines(tmp_path, UNKNOWN) == {2: LOW}  # reported for review, never proven


def test_an_unknown_function_explains_how_to_declare_it(tmp_path):
    (tmp_path / "client.py").write_text(UNKNOWN)
    [usage] = find_usages(tmp_path, SIZE_REMOVED)
    assert "track(), which apimend does not recognise as an HTTP client" in usage.reasons[0]
    assert "--request-function track" in usage.reasons[0]


def test_an_unknown_function_is_never_changed(tmp_path):
    (tmp_path / "client.py").write_text(UNKNOWN)
    assert plan_fixes([SIZE_REMOVED, SIZE_REQUIRED], tmp_path) == []


def test_a_declared_function_is_a_request(tmp_path):
    assert lines(tmp_path, UNKNOWN, request_functions=["track"]) == {2: HIGH}


def test_request_functions_can_be_declared_in_pyproject(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[tool.apimend]\nrequest-functions = ["track"]\n')
    (tmp_path / "client.py").write_text(UNKNOWN)
    assert main(["scan", V1, V2, str(tmp_path)]) == 1
    assert "[high]" in capsys.readouterr().out


def test_an_invalid_pyproject_setting_is_an_error(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text("[tool.apimend]\nrequest-functions = 'track'\n")
    assert main(["scan", V1, V2, str(tmp_path)]) == 2
    assert "must be a list of function names" in capsys.readouterr().err


# --- known HTTP clients, recognised through imports --------------------------


@pytest.mark.parametrize(
    "source",
    [
        'import requests\nrequests.post("/orders", json={"size": 1})\n',
        'import requests as rq\nrq.post("/orders", json={"size": 1})\n',
        'from requests import post\npost("/orders", json={"size": 1})\n',
        'import httpx\nhttpx.post("/orders", json={"size": 1})\n',
        'import requests\nrequests.request("POST", "/orders", json={"size": 1})\n',
        'import requests\ns = requests.Session()\ns.post("/orders", json={"size": 1})\n',
        'import httpx\nwith httpx.Client() as c:\n    c.post("/orders", json={"size": 1})\n',
        (
            "import aiohttp\nasync def f():\n    async with aiohttp.ClientSession() as s:\n"
            '        await s.post("/orders", json={"size": 1})\n'
        ),
        'import httpx\ndef f(client: httpx.Client):\n    client.post("/orders", json={"size": 1})\n',
        (
            "import json, urllib.request\n"
            'urllib.request.Request("/orders", data=json.dumps({"size": 1}).encode())\n'
        ),
        (
            "import http.client, json\nconn = http.client.HTTPSConnection('api.example.com')\n"
            'conn.request("POST", "/orders", body=json.dumps({"size": 1}))\n'
        ),
    ],
)
def test_known_http_clients_are_requests(tmp_path, source):
    assert list(lines(tmp_path, source).values()) == [HIGH]


def test_post_without_the_import_is_not_a_request():
    assert requests_in('requests.post("/orders", json={"size": 1})\n') == []


def test_an_unannotated_object_is_not_assumed_to_be_an_http_client(tmp_path):
    source = 'def f(client):\n    client.post("/orders", json={"size": 1})\n'
    assert requests_in(source) == []
    assert lines(tmp_path, source) == {2: LOW}


def test_a_client_stored_on_self_is_recognised():
    source = (
        "import requests\n"
        "class Api:\n"
        "    def __init__(self):\n"
        "        self.session = requests.Session()\n"
        "    def order(self):\n"
        '        return self.session.post("/orders", json={"size": 1})\n'
    )
    assert requests_in(source) == [(6, "POST", ["/orders"])]


def test_urllib_method_follows_urllib_rules():
    source = (
        "import urllib.request\n"
        'urllib.request.Request("/menu")\n'
        'urllib.request.Request("/orders", data=b"{}")\n'
        'urllib.request.Request("/orders", data=b"{}", method="PUT")\n'
    )
    methods = [method for _, method, _ in requests_in(source)]
    assert methods == ["GET", "POST", "PUT"]


# --- wrappers: proven when they pass a parameter into a real request ----------


def test_a_wrapper_is_proven_from_its_code(tmp_path):
    source = (
        "import requests\n"
        'BASE = "https://api.example.com"\n'
        "def send(path, body):\n"
        "    return requests.post(BASE + path, json=body)\n"
        "def order(size):\n"
        '    return send("/orders", {"size": size})\n'
    )
    assert lines(tmp_path, source) == {6: HIGH}  # no declaration needed


def test_a_wrapper_method_with_the_http_method_as_a_parameter(tmp_path):
    source = (
        "import requests\n"
        "class Api:\n"
        "    def __init__(self):\n"
        "        self.session = requests.Session()\n"
        "    def call(self, method, path, body=None):\n"
        "        return self.session.request(method, path, json=body)\n"
        "def order(api, size):\n"
        '    return api.call("POST", "/orders", {"size": size})\n'
        "def menu(api):\n"
        '    return api.call("GET", "/orders")\n'
    )
    assert requests_in(source)[-2:] == [(8, "POST", ["/orders"]), (10, "GET", ["/orders"])]
    assert lines(tmp_path, source) == {8: HIGH}  # the GET call is a different operation


def test_a_function_whose_url_does_not_come_from_its_parameters_is_not_a_wrapper(tmp_path):
    source = (
        "import requests\n"
        "def audit(path, details):\n"
        '    requests.post("/audit", json={"path": path})\n'
        "def order(size):\n"
        '    audit("/orders", {"size": size})\n'
    )
    assert [paths for _, _, paths in requests_in(source)] == [["/audit"]]
    assert HIGH not in lines(tmp_path, source).values()


def test_the_lab_wrapper_is_proven_without_any_settings():
    source = (LAB / "app" / "pizza_client.py").read_text()
    assert (34, "POST", ["/orders"]) in requests_in(source)
