"""Tests for oraplanviz_agent.server -- spins up a real ThreadingHTTPServer
on an ephemeral port with a fake Db, and drives it via stdlib urllib.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from oraplanviz_agent.db import DbError
from oraplanviz_agent.server import create_server

TOKEN = "test-token-123"
ALLOWED_ORIGIN = "http://localhost:5173"


class FakeDb:
    def __init__(self):
        self.connected = False
        self.oracle_version = None
        self.connect_calls = []
        self.disconnect_calls = 0

    @property
    def is_connected(self):
        return self.connected

    def connect(self, dsn, user, password):
        self.connect_calls.append((dsn, user, password))
        if password == "wrong":
            raise DbError("Failed to connect: bad password", 502)
        self.connected = True
        self.oracle_version = "19.27.0.0.0"

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False
        self.oracle_version = None

    def recent_sql(self, source):
        if source == "cursor":
            return [{"sqlId": "abc123", "sqlText": "select 1"}]
        return [{"sqlId": "abc123", "sqlExecId": 1}]

    def fetch_plan(self, sql_id, source, child_number=0, sql_exec_id=None):
        if sql_id == "missing00000000000":
            raise DbError("No plan found for the given sql_id", 404)
        return f"PLAN TEXT for {sql_id} via {source}"


@pytest.fixture
def running_server():
    db = FakeDb()
    server = create_server(db, token=TOKEN, allowed_origins=[ALLOWED_ORIGIN], port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(url, method="GET", body=None, headers=None):
    headers = headers or {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
        return exc.code, dict(exc.headers), payload


def test_health_without_token(running_server):
    base_url, db = running_server
    status, _headers, payload = _request(f"{base_url}/api/health")
    assert status == 200
    assert payload["connected"] is False
    assert payload["oracleVersion"] is None
    assert "version" in payload


def test_plan_requires_token(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(f"{base_url}/api/plan?sqlId=abc123&source=cursor")
    assert status == 401
    assert "error" in payload


def test_cors_headers_for_allowed_origin(running_server):
    base_url, _db = running_server
    status, headers, _payload = _request(
        f"{base_url}/api/health", headers={"Origin": ALLOWED_ORIGIN}
    )
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
    assert headers.get("Vary") == "Origin"


def test_cors_headers_absent_for_disallowed_origin(running_server):
    base_url, _db = running_server
    status, headers, _payload = _request(
        f"{base_url}/api/health", headers={"Origin": "http://evil.example"}
    )
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers


def test_preflight_with_private_network_header(running_server):
    base_url, _db = running_server
    req = urllib.request.Request(
        f"{base_url}/api/plan",
        method="OPTIONS",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Private-Network": "true",
            "Access-Control-Request-Method": "GET",
        },
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Private-Network") == "true"
        assert resp.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN


def test_connect_flow_happy_path(running_server):
    base_url, db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/connect",
        method="POST",
        body={"dsn": "host:1521/pdb1", "user": "planviz", "password": "secret"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["oracleVersion"] == "19.27.0.0.0"
    assert db.connected is True

    status, _headers, payload = _request(f"{base_url}/api/health")
    assert payload["connected"] is True

    status, _headers, payload = _request(
        f"{base_url}/api/disconnect",
        method="POST",
        body={},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert db.connected is False


def test_connect_bad_password_maps_to_502(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/connect",
        method="POST",
        body={"dsn": "host:1521/pdb1", "user": "planviz", "password": "wrong"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 502
    assert "error" in payload


def test_recent_sql(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/sql/recent?source=cursor",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert payload["items"][0]["sqlId"] == "abc123"


def test_recent_sql_bad_source(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/sql/recent?source=bogus",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert "error" in payload


def test_fetch_plan_returns_text(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/plan?sqlId=abc123&source=cursor",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert payload["source"] == "cursor"
    assert "PLAN TEXT for abc123 via cursor" in payload["text"]


def test_fetch_plan_bad_source(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/plan?sqlId=abc123&source=bogus",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert "error" in payload


def test_fetch_plan_missing_sql_id(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/plan?source=cursor",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert "error" in payload


def test_unknown_path_404(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/nope",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 404
    assert "error" in payload
