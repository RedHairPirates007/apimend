"""A tiny 'customer app' that uses the Pizza API.

It was written for v1 of the API. When the API moves to v2, this code
breaks - and that's exactly what apimend should notice and fix.
"""

from __future__ import annotations

import json
import os
import urllib.request

BASE_URL = os.environ.get("PIZZA_API_URL", "http://127.0.0.1:4010")


def _request(method: str, path: str, body: dict | None = None) -> dict | list:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get_menu() -> list:
    return _request("GET", "/menu")


def place_order(pizza: str, size: str) -> dict:
    # Written for API v1, which expects the field name "size".
    return _request("POST", "/orders", {"pizza": pizza, "size": size})