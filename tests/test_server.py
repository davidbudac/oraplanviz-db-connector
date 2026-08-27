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

    def fetch_metadata(self, sql_id, plan_hash=None):
        self.metadata_calls = getattr(self, "metadata_calls", [])
        self.metadata_calls.append((sql_id, plan_hash))
        if sql_id == "brokenjson000":
            return "this is not json {"
        return (
            '{"format":"ora-plan-metadata","version":2,'
            '"plan_ref":{"sql_id":"%s","plan_hash_value":%s},'
            '"objects":{},"coverage_warnings":[]}'
            % (sql_id, "null" if plan_hash is None else plan_hash)
        )


class FakeTestDb:
    """Stand-in for TestDb: records what the /api/test/* routes asked for."""

    def __init__(self):
        self.connected = False
        self.oracle_version = None
        self.connect_calls = []
        self.disconnect_calls = 0
        self.scripts = []
        self.explains = []
        self.statement_log = [{"seq": 1, "statement": "SELECT 1 FROM dual", "ok": True}]

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

    def exec_script(self, script):
        self.scripts.append(script)
        if not self.connected:
            raise DbError("test connection not open", 409)
        if "boom" in script:
            return {"ok": False, "output": "SQL> boom", "errors": ["line 1: ORA-00942"]}
        return {"ok": True, "output": "Table created.", "errors": []}

    def explain(self, sql):
        self.explains.append(sql)
        if not self.connected:
            raise DbError("test connection not open", 409)
        if "bad" in sql:
            raise DbError("EXPLAIN PLAN failed: ORA-00942", 400)
        return "Plan hash value: 42"


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


@pytest.fixture
def running_test_server():
    """Server whose /api/test/* routes are backed by a FakeTestDb."""
    db = FakeDb()
    test_db = FakeTestDb()
    server = create_server(
        db, token=TOKEN, allowed_origins=[ALLOWED_ORIGIN], port=0, test_db=test_db
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", db, test_db
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


def test_metadata_returns_bundle(running_server):
    base_url, db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/metadata?sqlId=abc123&planHash=987654321",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert payload["bundle"]["format"] == "ora-plan-metadata"
    assert payload["bundle"]["plan_ref"]["plan_hash_value"] == 987654321
    assert db.metadata_calls == [("abc123", 987654321)]


def test_metadata_requires_token(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(f"{base_url}/api/metadata?sqlId=abc123")
    assert status == 401
    assert "error" in payload


def test_metadata_missing_sql_id(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/metadata",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert "error" in payload


def test_metadata_bad_plan_hash(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/metadata?sqlId=abc123&planHash=xyz",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert "error" in payload


def test_metadata_invalid_bundle_json_maps_to_500(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/metadata?sqlId=brokenjson000",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 500
    assert "error" in payload


# -- /api/test/* (approval-gated script execution) --------------------------


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_test_connect_uses_the_test_db_not_the_source_db(running_test_server):
    base_url, db, test_db = running_test_server
    status, _headers, payload = _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "host:1521/scratch", "user": "eval", "password": "pw"},
        headers=_auth(),
    )
    assert status == 200
    assert payload == {"ok": True, "oracleVersion": "19.27.0.0.0"}
    assert test_db.connect_calls == [("host:1521/scratch", "eval", "pw")]
    # The read-only source connection was untouched.
    assert db.connect_calls == []
    assert db.connected is False


def test_test_connect_requires_all_credentials(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, payload = _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "host:1521/scratch"},
        headers=_auth(),
    )
    assert status == 400
    assert "error" in payload


def test_test_connect_failure_maps_to_502(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, _payload = _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "host:1521/scratch", "user": "eval", "password": "wrong"},
        headers=_auth(),
    )
    assert status == 502


def test_test_endpoints_require_a_token(running_test_server):
    base_url, _db, _test_db = running_test_server
    for path, body in (
        ("/api/test/connect", {"dsn": "d", "user": "u", "password": "p"}),
        ("/api/test/exec", {"script": "SELECT 1 FROM dual;"}),
        ("/api/test/explain", {"sql": "SELECT 1 FROM dual"}),
        ("/api/test/disconnect", {}),
    ):
        status, _headers, payload = _request(f"{base_url}{path}", method="POST", body=body)
        assert status == 401, path
        assert "error" in payload


def test_test_exec_returns_ok_output_errors(running_test_server):
    base_url, _db, test_db = running_test_server
    _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "d", "user": "u", "password": "p"},
        headers=_auth(),
    )

    status, _headers, payload = _request(
        f"{base_url}/api/test/exec",
        method="POST",
        body={"script": "CREATE TABLE t (id NUMBER);"},
        headers=_auth(),
    )
    assert status == 200
    assert payload == {"ok": True, "output": "Table created.", "errors": []}
    assert test_db.scripts == ["CREATE TABLE t (id NUMBER);"]


def test_test_exec_reports_statement_failures_in_band(running_test_server):
    base_url, _db, _test_db = running_test_server
    _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "d", "user": "u", "password": "p"},
        headers=_auth(),
    )

    status, _headers, payload = _request(
        f"{base_url}/api/test/exec",
        method="POST",
        body={"script": "boom;"},
        headers=_auth(),
    )
    # A failed statement is still a successful request: the caller wants the
    # transcript and the error text.
    assert status == 200
    assert payload["ok"] is False
    assert payload["errors"] == ["line 1: ORA-00942"]


def test_test_exec_without_a_connection_is_409(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, payload = _request(
        f"{base_url}/api/test/exec",
        method="POST",
        body={"script": "SELECT 1 FROM dual;"},
        headers=_auth(),
    )
    assert status == 409
    assert payload["error"] == "test connection not open"


def test_test_exec_requires_a_script(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, payload = _request(
        f"{base_url}/api/test/exec", method="POST", body={"script": "   "}, headers=_auth()
    )
    assert status == 400
    assert "error" in payload


def test_test_explain_returns_dbms_xplan_text(running_test_server):
    base_url, _db, test_db = running_test_server
    _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "d", "user": "u", "password": "p"},
        headers=_auth(),
    )

    status, _headers, payload = _request(
        f"{base_url}/api/test/explain",
        method="POST",
        body={"sql": "SELECT 1 FROM dual"},
        headers=_auth(),
    )
    assert status == 200
    assert payload == {"dbmsXplanText": "Plan hash value: 42"}
    assert test_db.explains == ["SELECT 1 FROM dual"]


def test_test_explain_bad_statement_maps_to_400(running_test_server):
    base_url, _db, _test_db = running_test_server
    _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "d", "user": "u", "password": "p"},
        headers=_auth(),
    )
    status, _headers, payload = _request(
        f"{base_url}/api/test/explain",
        method="POST",
        body={"sql": "SELECT * FROM bad"},
        headers=_auth(),
    )
    assert status == 400
    assert "ORA-00942" in payload["error"]


def test_test_disconnect(running_test_server):
    base_url, _db, test_db = running_test_server
    _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "d", "user": "u", "password": "p"},
        headers=_auth(),
    )
    status, _headers, payload = _request(
        f"{base_url}/api/test/disconnect", method="POST", body={}, headers=_auth()
    )
    assert status == 200
    assert payload == {"ok": True}
    assert test_db.disconnect_calls == 1
    assert test_db.connected is False


def test_health_reports_the_test_connection_separately(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, payload = _request(f"{base_url}/api/health")
    assert status == 200
    assert payload["connected"] is False
    assert payload["testConnected"] is False

    _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": "d", "user": "u", "password": "p"},
        headers=_auth(),
    )
    _status, _headers, payload = _request(f"{base_url}/api/health")
    # Source connection still closed; only the test one opened.
    assert payload["connected"] is False
    assert payload["testConnected"] is True


def test_test_log_lists_executed_statements(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, payload = _request(f"{base_url}/api/test/log", headers=_auth())
    assert status == 200
    assert payload["items"][0]["statement"] == "SELECT 1 FROM dual"


def test_test_log_requires_a_token(running_test_server):
    base_url, _db, _test_db = running_test_server
    status, _headers, _payload = _request(f"{base_url}/api/test/log")
    assert status == 401


def test_server_creates_its_own_test_db_when_none_is_given(running_server):
    """create_server() must never route /api/test/* at the source connection."""
    base_url, db = running_server
    _request(
        f"{base_url}/api/connect",
        method="POST",
        body={"dsn": "host:1521/pdb1", "user": "planviz", "password": "secret"},
        headers=_auth(),
    )
    assert db.connected is True

    # The source connection being open does not open the test one.
    status, _headers, payload = _request(
        f"{base_url}/api/test/exec",
        method="POST",
        body={"script": "SELECT 1 FROM dual;"},
        headers=_auth(),
    )
    assert status == 409
    assert payload["error"] == "test connection not open"


def test_full_test_stack_over_http(monkeypatch):
    """The real TestDb, driven over HTTP exactly as the frontend client does.

    Mirrors src/lib/agent/client.ts: same paths, same request bodies, same
    response shapes (`agentTestApi.test.ts` asserts these on the other side).
    """
    from test_test_db import FakeOracledb  # shared fake driver

    from oraplanviz_agent import db as db_module
    from oraplanviz_agent.db import TestDb

    monkeypatch.setattr(db_module, "oracledb", FakeOracledb())

    db = FakeDb()
    server = create_server(
        db, token=TOKEN, allowed_origins=[ALLOWED_ORIGIN], port=0, test_db=TestDb()
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, _headers, payload = _request(
            f"{base_url}/api/test/connect",
            method="POST",
            body={"dsn": "//host:1521/scratch", "user": "eval", "password": "pw"},
            headers=_auth(),
        )
        assert status == 200
        assert payload == {"ok": True, "oracleVersion": "19.27.0.0.0"}

        status, _headers, payload = _request(
            f"{base_url}/api/test/exec",
            method="POST",
            body={"script": "CREATE TABLE t (id NUMBER);\nSELECT id, name FROM t;\n"},
            headers=_auth(),
        )
        assert status == 200
        assert set(payload) == {"ok", "output", "errors"}
        assert payload["ok"] is True
        assert payload["errors"] == []
        assert "Table created." in payload["output"]
        assert "2 rows selected." in payload["output"]

        status, _headers, payload = _request(
            f"{base_url}/api/test/explain",
            method="POST",
            body={"sql": "SELECT * FROM t WHERE id = :id"},
            headers=_auth(),
        )
        assert status == 200
        assert list(payload) == ["dbmsXplanText"]
        assert payload["dbmsXplanText"].startswith("Plan hash value:")

        status, _headers, payload = _request(f"{base_url}/api/test/log", headers=_auth())
        assert status == 200
        assert [item["kind"] for item in payload["items"]] == ["sql", "sql", "explain"]
        assert all(item["ok"] for item in payload["items"])

        status, _headers, payload = _request(
            f"{base_url}/api/test/disconnect", method="POST", body={}, headers=_auth()
        )
        assert status == 200
        assert payload == {"ok": True}

        # ...and the source connection was never touched along the way.
        assert db.connect_calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_unknown_path_404(running_server):
    base_url, _db = running_server
    status, _headers, payload = _request(
        f"{base_url}/api/nope",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 404
    assert "error" in payload
