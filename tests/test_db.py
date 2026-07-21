"""Tests for oraplanviz_agent.db -- runs without the real oracledb installed
by monkeypatching the module-level `oracledb` attribute with a fake driver.
"""

from __future__ import annotations

import pytest

from oraplanviz_agent import db as db_module
from oraplanviz_agent.db import Db, DbError


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows = []
        self.executed = []

    def execute(self, sql, **binds):
        self.executed.append((sql, binds))
        normalized = " ".join(sql.split())

        if "v$sql_monitor" not in sql.lower() and "v$sql" in sql.lower():
            self.description = [
                ("sql_id",),
                ("child_number",),
                ("plan_hash_value",),
                ("sql_text",),
                ("elapsed_sec",),
                ("executions",),
                ("last_active_time",),
            ]
            self._rows = [("abc123sqlid", 0, 12345, "select 1 from dual", 1.23, 4, "2026-07-17T00:00:00")]
        elif "v$sql_monitor" in sql.lower():
            self.description = [
                ("key",),
                ("sql_id",),
                ("sql_exec_id",),
                ("sql_plan_hash_value",),
                ("status",),
                ("sql_text",),
                ("elapsed_sec",),
                ("sql_exec_start",),
            ]
            self._rows = [
                (
                    1,
                    "abc123sqlid",
                    999,
                    12345,
                    "DONE",
                    "select 1 from dual",
                    1.23,
                    "2026-07-17T00:00:00",
                )
            ]
        elif "display_cursor" in normalized.lower():
            assert "sql_id" in binds
            assert "child_number" in binds
            self._rows = [("Plan hash value: 123",), ("| Id | Operation |",)]
        elif "display_awr" in normalized.lower():
            assert "sql_id" in binds
            self._rows = [("Plan hash value: 456",), ("| Id | Operation |",)]
        elif "report_sql_monitor" in normalized.lower():
            assert "sql_id" in binds
            assert "sql_exec_id" in binds
            self._rows = [("<report>xml</report>",)]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, user, password, dsn):
        self.user = user
        self.password = password
        self.dsn = dsn
        self.version = "19.27.0.0.0"

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


class FakeOracledb:
    def __init__(self):
        self.connect_calls = []

    def connect(self, user, password, dsn):
        self.connect_calls.append({"user": user, "password": password, "dsn": dsn})
        if password == "wrongpassword":
            raise Exception("ORA-01017: invalid username/password")
        return FakeConnection(user, password, dsn)


@pytest.fixture(autouse=True)
def fake_driver(monkeypatch):
    fake = FakeOracledb()
    monkeypatch.setattr(db_module, "oracledb", fake)
    return fake


def test_not_connected_raises_409():
    db = Db()
    with pytest.raises(DbError) as exc_info:
        db.recent_sql("cursor")
    assert exc_info.value.status_code == 409


def test_connect_uses_binds_and_sets_version(fake_driver):
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    assert db.is_connected
    assert db.oracle_version == "19.27.0.0.0"
    assert fake_driver.connect_calls == [{"user": "planviz", "password": "secret", "dsn": "host:1521/pdb1"}]


def test_connect_failure_raises_502(fake_driver):
    db = Db()
    with pytest.raises(DbError) as exc_info:
        db.connect("host:1521/pdb1", "planviz", "wrongpassword")
    assert exc_info.value.status_code == 502
    assert not db.is_connected


def test_disconnect_resets_state():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    db.disconnect()
    assert not db.is_connected
    assert db.oracle_version is None


def test_recent_sql_cursor_maps_camelcase():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    items = db.recent_sql("cursor")
    assert items == [
        {
            "sqlId": "abc123sqlid",
            "childNumber": 0,
            "planHashValue": 12345,
            "sqlText": "select 1 from dual",
            "elapsedSec": 1.23,
            "executions": 4,
            "lastActive": "2026-07-17T00:00:00",
        }
    ]


def test_recent_sql_monitor_maps_camelcase():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    items = db.recent_sql("monitor")
    assert items == [
        {
            "sqlId": "abc123sqlid",
            "sqlExecId": 999,
            "planHashValue": 12345,
            "status": "DONE",
            "sqlText": "select 1 from dual",
            "elapsedSec": 1.23,
            "lastActive": "2026-07-17T00:00:00",
        }
    ]


def test_recent_sql_invalid_source():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    with pytest.raises(DbError) as exc_info:
        db.recent_sql("bogus")
    assert exc_info.value.status_code == 400


def test_fetch_plan_cursor_joins_rows_with_newline():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    text = db.fetch_plan("abc123sqlid", "cursor", child_number=0)
    assert text == "Plan hash value: 123\n| Id | Operation |"


def test_fetch_plan_awr_joins_rows_with_newline():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    text = db.fetch_plan("abc123sqlid", "awr")
    assert text == "Plan hash value: 456\n| Id | Operation |"


def test_fetch_plan_monitor_returns_clob_text():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    text = db.fetch_plan("abc123sqlid", "monitor", sql_exec_id=999)
    assert text == "<report>xml</report>"


def test_fetch_plan_invalid_source():
    db = Db()
    db.connect("host:1521/pdb1", "planviz", "secret")
    with pytest.raises(DbError) as exc_info:
        db.fetch_plan("abc123sqlid", "bogus")
    assert exc_info.value.status_code == 400


def test_fetch_plan_not_connected():
    db = Db()
    with pytest.raises(DbError) as exc_info:
        db.fetch_plan("abc123sqlid", "cursor")
    assert exc_info.value.status_code == 409
