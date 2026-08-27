"""Live end-to-end tests against a real Oracle database.

Skipped unless connection env vars are set (they never run in the default
unit-test suite):

    ORAPLANVIZ_E2E_DSN=//host:1521/service \
    ORAPLANVIZ_E2E_USER=planviz \
    ORAPLANVIZ_E2E_PASSWORD=... \
    pytest -q tests/test_e2e_live.py

The suite starts the real agent server with a real Db, seeds the cursor
cache with a uniquely tagged MONITOR-hinted query over a second connection,
then exercises every endpoint through HTTP exactly like the browser panel
would.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
import uuid

import pytest

DSN = os.environ.get("ORAPLANVIZ_E2E_DSN")
USER = os.environ.get("ORAPLANVIZ_E2E_USER")
PASSWORD = os.environ.get("ORAPLANVIZ_E2E_PASSWORD")

pytestmark = pytest.mark.skipif(
    not (DSN and USER and PASSWORD),
    reason="ORAPLANVIZ_E2E_DSN/USER/PASSWORD not set",
)

TOKEN = "e2e-live-token"


def _request(url, method="GET", body=None, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture(scope="module")
def agent():
    """Real agent server on an ephemeral port, connected to the live DB."""
    from oraplanviz_agent.db import Db
    from oraplanviz_agent.server import create_server

    db = Db()
    server = create_server(
        db, token=TOKEN, allowed_origins=["http://localhost:5173"], port=0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.disconnect()


@pytest.fixture(scope="module")
def seeded_sql_id(agent):
    """Run a uniquely tagged query directly (as the browser user would have
    run their workload) and return its sql_id from v$sql."""
    import oracledb

    tag = f"oraplanviz-e2e-{uuid.uuid4().hex[:12]}"
    query = (
        f"SELECT /*+ MONITOR */ /* {tag} */ owner, COUNT(*) "
        "FROM all_objects GROUP BY owner ORDER BY owner"
    )
    with oracledb.connect(user=USER, password=PASSWORD, dsn=DSN) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        cursor.fetchall()
        cursor.execute(
            "SELECT sql_id, plan_hash_value FROM v$sql "
            "WHERE sql_text LIKE :pat AND sql_text NOT LIKE '%v$sql%'",
            pat=f"%{tag}%",
        )
        row = cursor.fetchone()
    assert row, f"Seeded query not found in v$sql (tag {tag})"
    return {"sqlId": row[0], "planHash": row[1]}


def test_health_before_connect(agent):
    base_url, _db = agent
    status, payload = _request(f"{base_url}/api/health", token="")
    assert status == 200
    assert payload["connected"] is False


def test_connect(agent):
    base_url, _db = agent
    status, payload = _request(
        f"{base_url}/api/connect",
        method="POST",
        body={"dsn": DSN, "user": USER, "password": PASSWORD},
    )
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["oracleVersion"]


def test_recent_sql_cursor(agent, seeded_sql_id):
    base_url, _db = agent
    status, payload = _request(f"{base_url}/api/sql/recent?source=cursor")
    assert status == 200, payload
    assert isinstance(payload["items"], list)
    assert payload["items"], "expected at least one recent SQL"


def test_plan_cursor(agent, seeded_sql_id):
    base_url, _db = agent
    status, payload = _request(
        f"{base_url}/api/plan?sqlId={seeded_sql_id['sqlId']}&source=cursor"
    )
    assert status == 200, payload
    text = payload["text"]
    # DBMS_XPLAN.DISPLAY_CURSOR header the frontend parser auto-detects.
    assert f"SQL_ID  {seeded_sql_id['sqlId']}" in text
    assert "Plan hash value" in text
    assert "| Id  |" in text


def test_plan_monitor(agent, seeded_sql_id):
    base_url, _db = agent
    status, payload = _request(
        f"{base_url}/api/plan?sqlId={seeded_sql_id['sqlId']}&source=monitor"
    )
    # Tuning Pack feature; tolerate absence but validate shape when present.
    if status == 200:
        assert payload["text"].lstrip().startswith("<")
        assert "report" in payload["text"][:200]
    else:
        assert status in (404, 502), payload


def test_metadata_bundle(agent, seeded_sql_id):
    base_url, _db = agent
    status, payload = _request(
        f"{base_url}/api/metadata?sqlId={seeded_sql_id['sqlId']}"
        f"&planHash={seeded_sql_id['planHash']}"
    )
    assert status == 200, payload
    bundle = payload["bundle"]
    assert bundle["format"] == "ora-plan-metadata"
    assert bundle["version"] == 2
    assert bundle["plan_ref"]["sql_id"] == seeded_sql_id["sqlId"]
    assert isinstance(bundle["objects"], dict)
    assert isinstance(bundle["coverage_warnings"], list)
    # The seeded query only touches fixed dictionary views, so objects may be
    # empty — but system_params/optimizer_env must be present in SQL_ID mode.
    assert "system_params" in bundle
    assert "optimizer_env" in bundle
    assert "sql_management" in bundle


def test_metadata_on_user_table(agent):
    """Seed a query against a real heap table so the bundle carries a TABLE
    object with column stats, then gather metadata for it."""
    import oracledb

    tag = f"oraplanviz-e2e-tbl-{uuid.uuid4().hex[:12]}"
    base_url, _db = agent
    with oracledb.connect(user=USER, password=PASSWORD, dsn=DSN) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT table_name FROM user_tables FETCH FIRST 1 ROWS ONLY")
        row = cursor.fetchone()
        if not row:
            pytest.skip("connected schema owns no tables")
        table = row[0]
        # FULL hint: a plain COUNT(*) can be answered by an index fast full
        # scan, leaving only an INDEX object in the plan.
        cursor.execute(f'SELECT /*+ FULL(t) */ /* {tag} */ COUNT(*) FROM "{table}" t')
        cursor.fetchall()
        cursor.execute(
            "SELECT sql_id FROM v$sql "
            "WHERE sql_text LIKE :pat AND sql_text NOT LIKE '%v$sql%'",
            pat=f"%{tag}%",
        )
        sql_row = cursor.fetchone()
    assert sql_row, f"Seeded table query not found in v$sql (tag {tag})"

    status, payload = _request(f"{base_url}/api/metadata?sqlId={sql_row[0]}")
    assert status == 200, payload
    bundle = payload["bundle"]
    tables = {
        name: obj
        for name, obj in bundle["objects"].items()
        if obj.get("type") == "TABLE"
    }
    assert tables, f"expected a TABLE object in bundle, got {list(bundle['objects'])}"
    table_obj = next(iter(tables.values()))
    assert "stats" in table_obj
    assert "columns" in table_obj
    assert "constraints" in table_obj


def test_disconnect(agent):
    base_url, _db = agent
    status, payload = _request(f"{base_url}/api/disconnect", method="POST", body={})
    assert status == 200
    status, payload = _request(f"{base_url}/api/health", token="")
    assert payload["connected"] is False


# -- /api/test/* -------------------------------------------------------------
#
# These run on the agent's separate test connection. They use the same
# credentials as the source connection for convenience, but a real user would
# point them at a scratch schema.


@pytest.fixture(scope="module")
def test_connection(agent):
    base_url, _db = agent
    status, payload = _request(
        f"{base_url}/api/test/connect",
        method="POST",
        body={"dsn": DSN, "user": USER, "password": PASSWORD},
    )
    assert status == 200, payload
    assert payload["ok"] is True
    try:
        yield base_url
    finally:
        _request(f"{base_url}/api/test/disconnect", method="POST", body={})


def test_test_connection_is_reported_separately(test_connection):
    base_url = test_connection
    status, payload = _request(f"{base_url}/api/health", token="")
    assert status == 200
    assert payload["testConnected"] is True


def test_test_exec_script_transcript(test_connection):
    base_url = test_connection
    script = (
        "SET SERVEROUTPUT ON\n"
        "SELECT 1 AS one FROM dual;\n"
        "BEGIN\n"
        "  DBMS_OUTPUT.PUT_LINE('hello from oraplanviz');\n"
        "END;\n"
        "/\n"
    )
    status, payload = _request(
        f"{base_url}/api/test/exec", method="POST", body={"script": script}
    )
    assert status == 200, payload
    assert payload["ok"] is True, payload["errors"]
    assert payload["errors"] == []
    output = payload["output"]
    assert "skipped SQL*Plus command" in output  # SET SERVEROUTPUT ON
    assert "1 row selected." in output
    assert "hello from oraplanviz" in output
    assert "PL/SQL procedure successfully completed." in output


def test_test_exec_reports_ora_errors_without_aborting(test_connection):
    base_url = test_connection
    script = (
        "SELECT * FROM a_table_that_does_not_exist_xyz;\n" "SELECT 2 AS two FROM dual;\n"
    )
    status, payload = _request(
        f"{base_url}/api/test/exec", method="POST", body={"script": script}
    )
    assert status == 200, payload
    assert payload["ok"] is False
    assert any("ORA-00942" in err for err in payload["errors"]), payload["errors"]
    # The second statement still ran.
    assert "1 row selected." in payload["output"]


def test_test_explain_with_bind_variables(test_connection):
    base_url = test_connection
    status, payload = _request(
        f"{base_url}/api/test/explain",
        method="POST",
        body={
            "sql": "SELECT /*+ FULL(t) */ COUNT(*) FROM all_objects t "
            "WHERE owner = :owner_name"
        },
    )
    assert status == 200, payload
    text = payload["dbmsXplanText"]
    assert "Plan hash value" in text
    assert "| Id  |" in text


def test_test_explain_bad_sql_is_400(test_connection):
    base_url = test_connection
    status, payload = _request(
        f"{base_url}/api/test/explain",
        method="POST",
        body={"sql": "SELECT * FROM a_table_that_does_not_exist_xyz"},
    )
    assert status == 400, payload
    assert "ORA-00942" in payload["error"]


def test_test_exec_repro_cycle_on_a_scratch_table(test_connection):
    """The Phase-8 loop in miniature: build a table, stat it, explain it."""
    base_url = test_connection
    table = f"OPV_E2E_{uuid.uuid4().hex[:8].upper()}"

    status, payload = _request(
        f"{base_url}/api/test/exec",
        method="POST",
        body={"script": f"CREATE TABLE {table} (id NUMBER, pad VARCHAR2(100));"},
    )
    assert status == 200, payload
    if not payload["ok"]:
        pytest.skip(f"cannot create tables in this schema: {payload['errors']}")

    try:
        script = (
            f"INSERT INTO {table} SELECT LEVEL, RPAD('x', 100, 'x') "
            "FROM dual CONNECT BY LEVEL <= 1000;\n"
            "COMMIT;\n"
            f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '{table}'); END;\n"
            "/\n"
            f"SELECT COUNT(*) AS cnt FROM {table};\n"
        )
        status, payload = _request(
            f"{base_url}/api/test/exec", method="POST", body={"script": script}
        )
        assert status == 200, payload
        assert payload["ok"] is True, payload["errors"]
        assert "1000 rows inserted." in payload["output"]
        assert "Commit complete." in payload["output"]
        assert "PL/SQL procedure successfully completed." in payload["output"]
        assert "1000" in payload["output"]

        status, payload = _request(
            f"{base_url}/api/test/explain",
            method="POST",
            body={"sql": f"SELECT * FROM {table} WHERE id = :id"},
        )
        assert status == 200, payload
        assert table in payload["dbmsXplanText"]

        status, payload = _request(f"{base_url}/api/test/log")
        assert status == 200
        statements = [item["statement"] for item in payload["items"]]
        assert any(s.startswith("CREATE TABLE") for s in statements)
        assert any(s.startswith("SELECT * FROM") for s in statements)
        assert all(item["durationMs"] is not None for item in payload["items"])
    finally:
        _request(
            f"{base_url}/api/test/exec",
            method="POST",
            body={"script": f"DROP TABLE {table} PURGE;"},
        )
