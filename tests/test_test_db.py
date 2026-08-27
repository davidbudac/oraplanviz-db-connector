"""Tests for oraplanviz_agent.db.TestDb -- the separate test connection used by
/api/test/*. Runs without the real oracledb driver by monkeypatching the
module-level `oracledb` attribute, exactly like tests/test_db.py.
"""

from __future__ import annotations

import re

import pytest

from oraplanviz_agent import db as db_module
from oraplanviz_agent.db import Db, DbError, TestDb


class FakeVar:
    def __init__(self, value=None):
        self._value = value

    def setvalue(self, value):
        self._value = value

    def getvalue(self):
        return self._value


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = 0
        self._rows = []
        self._vars = []
        self._prepared = None

    # -- driver surface used by TestDb ------------------------------------

    def var(self, typ, size=None):
        var = FakeVar(None if typ is str else 0)
        self._vars.append((typ, size, var))
        return var

    def prepare(self, sql):
        self._prepared = sql

    def bindnames(self):
        # Mirrors the driver's own scan closely enough for these tests.
        return [m.group(1).upper() for m in re.finditer(r":([A-Za-z0-9_]+)", self._prepared)]

    def callproc(self, name, args):
        self.connection.callproc_calls.append((name, args))
        if name == "dbms_output.get_line":
            line_var, status_var = args
            queue = self.connection.dbms_output
            if queue:
                line_var.setvalue(queue.pop(0))
                status_var.setvalue(0)
            else:
                line_var.setvalue(None)
                status_var.setvalue(1)

    def execute(self, sql, params=None, **binds):
        if sql is None:  # re-execute of a prepared statement
            sql = self._prepared
            if isinstance(params, dict):
                binds = dict(params)
            elif params is not None:
                binds = {str(i + 1): v for i, v in enumerate(params)}
        self.connection.executed.append((sql, binds))
        upper = " ".join(sql.upper().split())

        if "RAISE_ERROR" in upper:
            raise Exception("ORA-00942: table or view does not exist")

        if upper.startswith("SELECT"):
            if "DBMS_XPLAN.DISPLAY" in upper:
                self.description = [("PLAN_TABLE_OUTPUT",)]
                self._rows = [("Plan hash value: 42",), ("| Id | Operation |",)]
            else:
                self.description = [("ID",), ("NAME",)]
                self._rows = [(1, "alpha"), (2, "beta")]
            self.rowcount = len(self._rows)
            return

        self.description = None
        self._rows = []
        self.rowcount = 3 if upper.startswith(("INSERT", "UPDATE", "DELETE")) else 0

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, size):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, user, password, dsn):
        self.user = user
        self.password = password
        self.dsn = dsn
        self.version = "19.27.0.0.0"
        self.executed = []
        self.callproc_calls = []
        self.dbms_output = []
        self.rollbacks = 0
        self.commits = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeOracledb:
    def __init__(self):
        self.connections = []

    def connect(self, user, password, dsn):
        if password == "wrongpassword":
            raise Exception("ORA-01017: invalid username/password")
        connection = FakeConnection(user, password, dsn)
        self.connections.append(connection)
        return connection


@pytest.fixture(autouse=True)
def fake_driver(monkeypatch):
    fake = FakeOracledb()
    monkeypatch.setattr(db_module, "oracledb", fake)
    return fake


@pytest.fixture
def test_db():
    db = TestDb()
    db.connect("host:1521/pdb1", "scratch", "secret")
    return db


def sql_only(connection):
    """Statements the fake connection actually received."""
    return [sql for sql, _binds in connection.executed]


# -- session separation -----------------------------------------------------


def test_test_connection_is_a_separate_session_from_the_source(fake_driver):
    source = Db()
    scratch = TestDb()
    source.connect("host:1521/pdb1", "readonly", "secret")
    scratch.connect("host:1521/pdb1", "scratch", "secret")

    assert len(fake_driver.connections) == 2
    assert source._connection is not scratch._connection
    assert not hasattr(source, "exec_script")


def test_exec_without_a_connection_is_409():
    with pytest.raises(DbError) as exc_info:
        TestDb().exec_script("SELECT 1 FROM dual;")
    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "test connection not open"


def test_explain_without_a_connection_is_409():
    with pytest.raises(DbError) as exc_info:
        TestDb().explain("SELECT 1 FROM dual")
    assert exc_info.value.status_code == 409


def test_disconnect_rolls_back_then_closes(test_db, fake_driver):
    connection = fake_driver.connections[0]
    test_db.exec_script("INSERT INTO t VALUES (1);")
    test_db.disconnect()

    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert connection.closed is True
    assert not test_db.is_connected


def test_connect_enables_dbms_output_and_resets_the_log(test_db, fake_driver):
    connection = fake_driver.connections[0]
    assert ("dbms_output.enable", [None]) in connection.callproc_calls

    test_db.exec_script("SELECT 1 FROM dual;")
    assert len(test_db.statement_log) == 1

    test_db.connect("host:1521/pdb1", "scratch", "secret")
    assert test_db.statement_log == []


# -- exec_script ------------------------------------------------------------


def test_exec_script_runs_every_statement_and_reports_ok(test_db, fake_driver):
    connection = fake_driver.connections[0]
    result = test_db.exec_script(
        "CREATE TABLE t (id NUMBER);\nINSERT INTO t VALUES (1);\n"
    )

    assert result["ok"] is True
    assert result["errors"] == []
    assert sql_only(connection) == [
        "CREATE TABLE t (id NUMBER)",
        "INSERT INTO t VALUES (1)",
    ]
    assert "Table created." in result["output"]
    assert "3 rows inserted." in result["output"]
    assert "SQL> CREATE TABLE t (id NUMBER)" in result["output"]


def test_exec_script_renders_query_results(test_db):
    result = test_db.exec_script("SELECT id, name FROM t;")
    assert result["ok"] is True
    assert "ID  NAME" in result["output"]
    assert "1   alpha" in result["output"]
    assert "2 rows selected." in result["output"]


def test_exec_script_continues_after_an_error_and_collects_it(test_db, fake_driver):
    connection = fake_driver.connections[0]
    result = test_db.exec_script(
        "SELECT * FROM raise_error;\nINSERT INTO t VALUES (1);\n"
    )

    assert result["ok"] is False
    assert len(result["errors"]) == 1
    assert "ORA-00942" in result["errors"][0]
    assert "line 1" in result["errors"][0]
    # The following statement still ran.
    assert "INSERT INTO t VALUES (1)" in sql_only(connection)
    assert "3 rows inserted." in result["output"]


def test_exec_script_notes_skipped_sqlplus_commands(test_db, fake_driver):
    connection = fake_driver.connections[0]
    result = test_db.exec_script("SET SERVEROUTPUT ON\nSELECT id FROM t;\n")

    assert "skipped SQL*Plus command" in result["output"]
    assert sql_only(connection) == ["SELECT id FROM t"]


def test_exec_script_captures_dbms_output(test_db, fake_driver):
    fake_driver.connections[0].dbms_output = ["hello", "world"]
    result = test_db.exec_script("BEGIN DBMS_OUTPUT.PUT_LINE('hello'); END;\n/\n")

    assert "hello\nworld" in result["output"]
    assert "PL/SQL procedure successfully completed." in result["output"]


def test_exec_script_keeps_dbms_output_from_a_failed_statement(test_db, fake_driver):
    fake_driver.connections[0].dbms_output = ["printed before the error"]
    result = test_db.exec_script("SELECT * FROM raise_error;\n")

    assert result["ok"] is False
    assert "printed before the error" in result["output"]
    assert "ORA-00942" in result["output"]


def test_a_second_call_while_one_is_running_is_409(test_db):
    # Simulates a concurrent request: the session lock is already held.
    assert test_db._busy.acquire(blocking=False)
    try:
        with pytest.raises(DbError) as exc_info:
            test_db.exec_script("SELECT 1 FROM dual;")
        assert exc_info.value.status_code == 409
        assert "already running" in exc_info.value.message

        with pytest.raises(DbError) as exc_info:
            test_db.explain("SELECT 1 FROM dual")
        assert exc_info.value.status_code == 409

        # A rejected call must not leave a half-finished log entry.
        assert test_db.statement_log == []
    finally:
        test_db._busy.release()

    # Released again -- the next call goes through.
    assert test_db.exec_script("SELECT id FROM t;")["ok"] is True


def test_exec_script_caps_returned_rows(test_db, monkeypatch):
    monkeypatch.setattr(TestDb, "MAX_ROWS", 1)
    result = test_db.exec_script("SELECT id, name FROM t;")
    assert "1 row selected. (output truncated)" in result["output"]


def test_exec_script_rejects_empty_and_oversized_scripts(test_db):
    with pytest.raises(DbError) as exc_info:
        test_db.exec_script("   \n")
    assert exc_info.value.status_code == 400

    with pytest.raises(DbError) as exc_info:
        test_db.exec_script("-- only a comment\n")
    assert exc_info.value.status_code == 400

    with pytest.raises(DbError) as exc_info:
        test_db.exec_script("x" * (TestDb.MAX_SCRIPT_CHARS + 1))
    assert exc_info.value.status_code == 413


def test_exec_script_never_commits_on_its_own(test_db, fake_driver):
    test_db.exec_script("INSERT INTO t VALUES (1);")
    assert fake_driver.connections[0].commits == 0


# -- explain ----------------------------------------------------------------


def test_explain_runs_explain_plan_then_display(test_db, fake_driver):
    connection = fake_driver.connections[0]
    text = test_db.explain("SELECT * FROM t;")

    statements = sql_only(connection)
    assert statements[0].startswith("EXPLAIN PLAN SET STATEMENT_ID = 'opv_")
    assert statements[0].endswith("FOR SELECT * FROM t")
    assert "DBMS_XPLAN.DISPLAY" in statements[1]
    assert statements[2].startswith("DELETE FROM plan_table")
    # The statement id is bound, never interpolated, on the read-back.
    assert connection.executed[1][1]["statement_id"].startswith("opv_")
    assert text == "Plan hash value: 42\n| Id | Operation |"


def test_explain_strips_a_trailing_slash_terminator(test_db, fake_driver):
    test_db.explain("SELECT * FROM t\n/\n")
    assert sql_only(fake_driver.connections[0])[0].endswith("FOR SELECT * FROM t")


def test_explain_binds_placeholders_to_null(test_db, fake_driver):
    """A statement full of binds must still explain (ORA-01008 otherwise)."""
    test_db.explain("SELECT * FROM t WHERE id = :b1 AND name = :b2")
    explain_sql, binds = fake_driver.connections[0].executed[0]
    assert explain_sql.endswith("FOR SELECT * FROM t WHERE id = :b1 AND name = :b2")
    assert binds == {"B1": None, "B2": None}


def test_explain_binds_numbered_placeholders_positionally(test_db, fake_driver):
    test_db.explain("SELECT * FROM t WHERE id = :1")
    _sql, binds = fake_driver.connections[0].executed[0]
    assert binds == {"1": None}


def test_explain_maps_a_bad_statement_to_400(test_db):
    with pytest.raises(DbError) as exc_info:
        test_db.explain("SELECT * FROM raise_error")
    assert exc_info.value.status_code == 400
    assert "ORA-00942" in exc_info.value.message


def test_explain_rejects_empty_sql(test_db):
    with pytest.raises(DbError) as exc_info:
        test_db.explain("  ;  ")
    assert exc_info.value.status_code == 400


# -- statement log ----------------------------------------------------------


def test_statement_log_records_every_statement(test_db):
    test_db.exec_script("CREATE TABLE t (id NUMBER);\nSELECT * FROM raise_error;\n")
    test_db.explain("SELECT * FROM t")

    log = test_db.statement_log
    assert [entry["seq"] for entry in log] == [1, 2, 3]
    assert log[0]["statement"] == "CREATE TABLE t (id NUMBER)"
    assert log[0]["ok"] is True
    assert log[0]["line"] == 1
    assert isinstance(log[0]["durationMs"], int)
    assert log[0]["startedAt"]

    assert log[1]["ok"] is False
    assert "ORA-00942" in log[1]["error"]

    assert log[2]["kind"] == "explain"
    assert log[2]["ok"] is True


def test_statement_log_is_a_copy(test_db):
    test_db.exec_script("SELECT id FROM t;")
    test_db.statement_log.clear()
    assert len(test_db.statement_log) == 1


def test_statement_log_is_bounded(test_db, monkeypatch):
    monkeypatch.setattr(TestDb, "MAX_LOG_ENTRIES", 3)
    test_db.exec_script("".join(f"SELECT {i} FROM dual;\n" for i in range(6)))
    log = test_db.statement_log
    assert len(log) == 3
    assert [entry["seq"] for entry in log] == [4, 5, 6]
