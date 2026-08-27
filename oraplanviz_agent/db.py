"""Oracle database access layer for the local DB-connect agent.

Uses python-oracledb in *thin* mode (pure Python, no Oracle Instant Client).
The `oracledb` driver is imported lazily on first use so that:
  - the CLI/server modules can be imported (and unit-tested) without the
    driver installed;
  - tests can monkeypatch the module-level `oracledb` attribute with a fake.

All SQL uses bind variables. User-supplied values (sql_id, dsn, user,
password, etc.) are NEVER interpolated into SQL text.

The one deliberate exception is `TestDb`, whose whole purpose is to run a
script the user explicitly approved in the UI: there the caller's text *is*
the statement. It runs on its own connection, never on the read-only `Db`
one, and every statement it executes is recorded in a statement log.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .script import (
    KIND_SKIPPED,
    Statement,
    feedback_for,
    format_result_set,
    split_statements,
)

logger = logging.getLogger("oraplanviz_agent.db")

# Lazily imported driver module. Tests monkeypatch this attribute directly,
# e.g. `oraplanviz_agent.db.oracledb = fake_oracledb`.
oracledb = None


def _ensure_driver():
    """Import the real oracledb driver on first use (thin mode by default).

    Never calls oracledb.init_oracle_client() -- thin mode is the default
    connection mode for python-oracledb and requires no Instant Client.
    """
    global oracledb
    if oracledb is None:
        import oracledb as _oracledb  # noqa: F401 (local import by design)

        oracledb = _oracledb
    return oracledb


class DbError(Exception):
    """Raised for any DB-agent-facing error, carrying an HTTP status code."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


_RECENT_SQL_QUERIES = {
    "cursor": """
        SELECT
            sql_id,
            child_number,
            plan_hash_value,
            SUBSTR(sql_text, 1, 200) AS sql_text,
            ROUND(elapsed_time / 1e6, 2) AS elapsed_sec,
            executions,
            last_active_time
        FROM v$sql
        WHERE sql_text NOT LIKE '%v$sql%'
          AND parsing_schema_name NOT IN ('SYS')
        ORDER BY last_active_time DESC
        FETCH FIRST 50 ROWS ONLY
    """,
    "monitor": """
        SELECT
            key,
            sql_id,
            sql_exec_id,
            sql_plan_hash_value,
            status,
            SUBSTR(sql_text, 1, 200) AS sql_text,
            ROUND(elapsed_time / 1e6, 2) AS elapsed_sec,
            sql_exec_start
        FROM v$sql_monitor
        ORDER BY sql_exec_start DESC
        FETCH FIRST 50 ROWS ONLY
    """,
}

_VALID_SOURCES = ("cursor", "monitor", "awr")


def _iso(value: Any) -> Optional[str]:
    """Best-effort ISO-8601 conversion for datetime-ish DB values."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


class BaseDb:
    """Holds a single Oracle connection: connect / disconnect / liveness.

    Two subclasses exist and they are deliberately never the same object:
    `Db` (the read-only *source* connection plans and metadata are read from)
    and `TestDb` (the *test* connection user-approved scripts run on). The
    source connection never executes AI- or user-authored SQL.
    """

    #: Message + status used when an endpoint is called with no connection open.
    _NOT_CONNECTED = ("Not connected to a database", 409)

    def __init__(self):
        self._connection = None
        self._oracle_version: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    @property
    def oracle_version(self) -> Optional[str]:
        return self._oracle_version

    def connect(self, dsn: str, user: str, password: str) -> None:
        driver = _ensure_driver()
        try:
            connection = driver.connect(user=user, password=password, dsn=dsn)
        except Exception as exc:  # noqa: BLE001 - surfaced as DbError
            raise DbError(f"Failed to connect: {exc}", 502) from exc

        self._connection = connection
        try:
            self._oracle_version = connection.version
        except Exception:  # noqa: BLE001 - version is best-effort
            self._oracle_version = None

    def disconnect(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass
        self._connection = None
        self._oracle_version = None

    def _require_connection(self):
        if self._connection is None:
            raise DbError(*self._NOT_CONNECTED)
        return self._connection


class Db(BaseDb):
    """The read-only *source* connection: recent SQL, plans, metadata bundles.

    Nothing here ever executes caller-supplied SQL text -- every statement is a
    fixed query with bind variables.
    """

    def recent_sql(self, source: str) -> List[Dict[str, Any]]:
        if source not in ("cursor", "monitor"):
            raise DbError(f"Invalid source for recent_sql: {source}", 400)

        connection = self._require_connection()
        query = _RECENT_SQL_QUERIES[source]

        try:
            cursor = connection.cursor()
            try:
                cursor.execute(query)
                columns = [d[0].lower() for d in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except DbError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DbError(f"Query failed: {exc}", 502) from exc

        items: List[Dict[str, Any]] = []
        for row in rows:
            record = dict(zip(columns, row))
            if source == "cursor":
                items.append(
                    {
                        "sqlId": record.get("sql_id"),
                        "childNumber": record.get("child_number"),
                        "planHashValue": record.get("plan_hash_value"),
                        "sqlText": record.get("sql_text"),
                        "elapsedSec": record.get("elapsed_sec"),
                        "executions": record.get("executions"),
                        "lastActive": _iso(record.get("last_active_time")),
                    }
                )
            else:
                items.append(
                    {
                        "sqlId": record.get("sql_id"),
                        "sqlExecId": record.get("sql_exec_id"),
                        "planHashValue": record.get("sql_plan_hash_value"),
                        "status": record.get("status"),
                        "sqlText": record.get("sql_text"),
                        "elapsedSec": record.get("elapsed_sec"),
                        "lastActive": _iso(record.get("sql_exec_start")),
                    }
                )
        return items

    def fetch_plan(
        self,
        sql_id: str,
        source: str,
        child_number: int = 0,
        sql_exec_id: Optional[int] = None,
    ) -> str:
        if source not in _VALID_SOURCES:
            raise DbError(f"Invalid plan source: {source}", 400)

        connection = self._require_connection()

        try:
            cursor = connection.cursor()
            try:
                if source == "cursor":
                    cursor.execute(
                        """
                        SELECT plan_table_output
                        FROM table(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :child_number, 'ALLSTATS LAST'))
                        """,
                        sql_id=sql_id,
                        child_number=child_number,
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise DbError("No plan found for the given sql_id", 404)
                    return "\n".join(row[0] or "" for row in rows)

                if source == "monitor":
                    cursor.execute(
                        """
                        SELECT DBMS_SQL_MONITOR.REPORT_SQL_MONITOR(
                                   sql_id => :sql_id,
                                   sql_exec_id => :sql_exec_id,
                                   type => 'XML',
                                   report_level => 'ALL'
                               )
                        FROM dual
                        """,
                        sql_id=sql_id,
                        sql_exec_id=sql_exec_id,
                    )
                    row = cursor.fetchone()
                    if not row or row[0] is None:
                        raise DbError("No SQL Monitor report found for the given sql_id", 404)
                    value = row[0]
                    read = getattr(value, "read", None)
                    return read() if callable(read) else str(value)

                # source == "awr"
                cursor.execute(
                    """
                    SELECT plan_table_output
                    FROM table(DBMS_XPLAN.DISPLAY_AWR(:sql_id, NULL, NULL, 'ALL'))
                    """,
                    sql_id=sql_id,
                )
                rows = cursor.fetchall()
                if not rows:
                    raise DbError("No AWR plan found for the given sql_id", 404)
                return "\n".join(row[0] or "" for row in rows)
            finally:
                cursor.close()
        except DbError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DbError(f"Query failed: {exc}", 502) from exc

    def fetch_metadata(self, sql_id: str, plan_hash: Optional[int] = None) -> str:
        """Run the vendored gather_plan_metadata.sql PL/SQL block and return
        the raw `ora-plan-metadata` bundle JSON text.

        The gather itself is best-effort by design: privilege gaps degrade to
        entries in the bundle's coverage_warnings, never to an error here.
        """
        from .metadata import get_metadata_block

        driver = _ensure_driver()
        connection = self._require_connection()
        block = get_metadata_block()

        try:
            cursor = connection.cursor()
            try:
                bundle_var = cursor.var(driver.DB_TYPE_CLOB)
                cursor.execute(
                    block,
                    arg1=sql_id,
                    arg2=None if plan_hash is None else str(plan_hash),
                    bundle=bundle_var,
                )
                value = bundle_var.getvalue()
            finally:
                cursor.close()
        except DbError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DbError(f"Metadata gather failed: {exc}", 502) from exc

        if value is None:
            raise DbError("Metadata gather returned no bundle", 500)
        read = getattr(value, "read", None)
        return read() if callable(read) else str(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _one_line(text: str, limit: int = 200) -> str:
    """Collapse a statement to a single line for the log."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 3] + "..."


class TestDb(BaseDb):
    """The separate *test* connection used for approved script execution.

    Distinct from `Db` by construction: a different instance, a different
    Oracle session. Guarantees this class provides:

      - every statement it runs is appended to `statement_log` (and logged to
        the agent console), so the user can audit what the AI actually ran;
      - nothing is ever committed on the caller's behalf -- `disconnect()`
        rolls back first, so an un-committed scratch session leaves no trace;
      - DBMS_OUTPUT written by a script is captured into the transcript.
    """

    _NOT_CONNECTED = ("test connection not open", 409)

    #: The name collides with pytest's `Test*` collection convention; this
    #: tells pytest the class is production code, not a test case.
    __test__ = False

    #: Guard rails. Scripts come from a model, so cap what one call can do.
    MAX_SCRIPT_CHARS = 200_000
    MAX_ROWS = 100
    MAX_OUTPUT_CHARS = 200_000
    MAX_LOG_ENTRIES = 500

    def __init__(self):
        super().__init__()
        self._log: List[Dict[str, Any]] = []
        self._seq = 0
        # The server is threaded and one Oracle session cannot run two things
        # at once; a double-clicked "Run" must not interleave statements.
        self._busy = threading.Lock()

    @property
    def statement_log(self) -> List[Dict[str, Any]]:
        """Statements executed on the current test session, oldest first."""
        return list(self._log)

    def connect(self, dsn: str, user: str, password: str) -> None:
        super().connect(dsn, user, password)
        # A connect starts a new test session, so the log starts empty.
        self._log = []
        self._seq = 0
        self._enable_dbms_output()
        logger.info("test session opened: %s@%s", user, dsn)

    def disconnect(self) -> None:
        connection = self._connection
        if connection is not None:
            # Nothing this session did with DML is kept unless the script
            # committed explicitly.
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - best-effort rollback
                logger.warning("test session rollback failed; closing anyway")
            logger.info("test session closed after %d statement(s)", len(self._log))
        super().disconnect()

    # -- execution ----------------------------------------------------------

    def exec_script(self, script: str) -> Dict[str, Any]:
        """Run a user-approved script and return `{ok, output, errors}`.

        Every statement is attempted, SQL*Plus style: a failure is recorded and
        execution continues, so one broken statement does not hide the result of
        the rest. `ok` is false when anything failed.
        """
        connection = self._require_connection()
        if not script or not script.strip():
            raise DbError("script is required", 400)
        if len(script) > self.MAX_SCRIPT_CHARS:
            raise DbError(
                f"Script too large ({len(script)} chars; limit {self.MAX_SCRIPT_CHARS})",
                413,
            )

        statements = split_statements(script)
        if not statements:
            raise DbError("script contains no statements", 400)

        chunks: List[str] = []
        errors: List[str] = []

        with self._exclusive():
            for statement in statements:
                if statement.kind == KIND_SKIPPED:
                    chunks.append(
                        f"-- skipped SQL*Plus command (line {statement.line}): {statement.text}"
                    )
                    continue

                chunks.append(f"SQL> {statement.text}")
                body, error = self._run_statement(connection, statement)
                if body:
                    chunks.append(body)
                if error:
                    errors.append(error)
                    chunks.append(error)
                chunks.append("")

        output = "\n".join(chunks).strip()
        if len(output) > self.MAX_OUTPUT_CHARS:
            output = (
                output[: self.MAX_OUTPUT_CHARS]
                + f"\n-- output truncated at {self.MAX_OUTPUT_CHARS} characters"
            )
        return {"ok": not errors, "output": output, "errors": errors}

    def explain(self, sql: str) -> str:
        """EXPLAIN PLAN the statement and return its DBMS_XPLAN.DISPLAY text."""
        connection = self._require_connection()
        if not sql or not sql.strip():
            raise DbError("sql is required", 400)

        text = _strip_terminator(sql)
        if not text:
            raise DbError("sql is required", 400)

        # Agent-generated, hex only -- safe to inline, and it keeps concurrent
        # explains on the same session from reading each other's rows.
        statement_id = "opv_" + uuid.uuid4().hex[:16]

        with self._exclusive():
            return self._explain_locked(connection, text, statement_id)

    def _explain_locked(self, connection, text: str, statement_id: str) -> str:
        # Logged only once the session is actually ours, so a rejected call
        # leaves no half-finished entry behind.
        started = time.monotonic()
        record = self._start_record("explain", text)
        try:
            cursor = connection.cursor()
            try:
                try:
                    self._execute_unbound(
                        cursor,
                        f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {text}",
                    )
                except Exception as exc:  # noqa: BLE001 - the statement is the input
                    raise DbError(f"EXPLAIN PLAN failed: {exc}", 400) from exc

                cursor.execute(
                    """
                    SELECT plan_table_output
                    FROM table(DBMS_XPLAN.DISPLAY('PLAN_TABLE', :statement_id, 'ALL'))
                    """,
                    statement_id=statement_id,
                )
                rows = cursor.fetchall()
                try:
                    cursor.execute(
                        "DELETE FROM plan_table WHERE statement_id = :statement_id",
                        statement_id=statement_id,
                    )
                except Exception:  # noqa: BLE001 - cleanup is best-effort
                    pass
            finally:
                cursor.close()
        except DbError as exc:
            self._finish_record(record, started, error=exc.message)
            raise
        except Exception as exc:  # noqa: BLE001
            self._finish_record(record, started, error=str(exc))
            raise DbError(f"Failed to display the plan: {exc}", 502) from exc

        if not rows:
            self._finish_record(record, started, error="EXPLAIN PLAN produced no output")
            raise DbError("EXPLAIN PLAN produced no output", 502)

        self._finish_record(record, started)
        return "\n".join(row[0] or "" for row in rows)

    # -- internals ----------------------------------------------------------

    @contextmanager
    def _exclusive(self):
        """Hold the test session for one call, or fail fast if it is busy."""
        if not self._busy.acquire(blocking=False):
            raise DbError("another statement is already running on the test connection", 409)
        try:
            yield
        finally:
            self._busy.release()

    @staticmethod
    def _execute_unbound(cursor, sql: str) -> None:
        """Execute a statement whose bind variables have no supplied values.

        EXPLAIN PLAN never peeks bind values, but the driver still refuses to
        send a statement with unbound placeholders (ORA-01008), and tuning SQL
        is full of binds. Bind them all to NULL and let them default to
        VARCHAR2: that is the one direction Oracle resolves *without* wrapping
        the column in a conversion, so the explained plan stays representative
        of the real one.
        """
        prepare = getattr(cursor, "prepare", None)
        bindnames = getattr(cursor, "bindnames", None)
        if not (callable(prepare) and callable(bindnames)):  # pragma: no cover
            cursor.execute(sql)
            return

        cursor.prepare(sql)
        try:
            names = [str(name) for name in (cursor.bindnames() or [])]
        except Exception:  # noqa: BLE001 - fall back to a plain execute
            names = []

        if not names:
            cursor.execute(None)
        elif all(name.isdigit() for name in names):
            cursor.execute(None, [None] * len(names))
        else:
            cursor.execute(None, {name: None for name in names})

    def _run_statement(self, connection, statement: Statement):
        """Execute one statement; returns `(transcript_body, error_or_None)`."""
        started = time.monotonic()
        record = self._start_record(statement.kind, statement.text, line=statement.line)
        body = None
        error = None
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(statement.text)
                if cursor.description:
                    columns = [d[0] for d in cursor.description]
                    rows, truncated = self._fetch_capped(cursor)
                    body = format_result_set(columns, rows, truncated)
                else:
                    body = feedback_for(statement, getattr(cursor, "rowcount", None))
            finally:
                cursor.close()
        except Exception as exc:  # noqa: BLE001 - reported in-band, not as HTTP 5xx
            error = f"line {statement.line}: {exc}"
            self._finish_record(record, started, error=str(exc))
        else:
            self._finish_record(record, started)

        # Drain even on failure: a block that printed before raising still has
        # something to say, and anything left behind would surface under the
        # next statement.
        printed = self._drain_dbms_output(connection)
        if printed:
            body = f"{printed}\n{body}" if body else printed
        return body, error

    def _fetch_capped(self, cursor):
        """Fetch at most MAX_ROWS rows, reporting whether more were available."""
        fetchmany = getattr(cursor, "fetchmany", None)
        if callable(fetchmany):
            rows = list(fetchmany(self.MAX_ROWS + 1))
        else:  # pragma: no cover - drivers all have fetchmany; fakes may not
            rows = list(cursor.fetchall())
        truncated = len(rows) > self.MAX_ROWS
        return rows[: self.MAX_ROWS], truncated

    def _enable_dbms_output(self) -> None:
        """Turn on DBMS_OUTPUT so a script's own diagnostics reach the caller."""
        connection = self._connection
        if connection is None:
            return
        try:
            cursor = connection.cursor()
            try:
                cursor.callproc("dbms_output.enable", [None])
            finally:
                cursor.close()
        except Exception:  # noqa: BLE001 - optional convenience, never fatal
            logger.debug("could not enable DBMS_OUTPUT on the test session")

    def _drain_dbms_output(self, connection) -> str:
        """Collect whatever the last statement printed with DBMS_OUTPUT."""
        lines: List[str] = []
        try:
            cursor = connection.cursor()
            try:
                # DBMS_OUTPUT lines are VARCHAR2(32767) in PL/SQL.
                line_var = cursor.var(str, 32767)
                status_var = cursor.var(int)
                while len(lines) < self.MAX_ROWS:
                    cursor.callproc("dbms_output.get_line", (line_var, status_var))
                    if status_var.getvalue() != 0:
                        break
                    lines.append(line_var.getvalue() or "")
            finally:
                cursor.close()
        except Exception:  # noqa: BLE001 - no DBMS_OUTPUT is not an error
            return ""
        return "\n".join(lines)

    # -- statement log ------------------------------------------------------

    def _start_record(self, kind: str, text: str, line: Optional[int] = None) -> Dict[str, Any]:
        self._seq += 1
        record: Dict[str, Any] = {
            "seq": self._seq,
            "kind": kind,
            "line": line,
            "statement": text,
            "startedAt": _utc_now_iso(),
            "durationMs": None,
            "ok": None,
            "error": None,
        }
        self._log.append(record)
        if len(self._log) > self.MAX_LOG_ENTRIES:
            del self._log[: len(self._log) - self.MAX_LOG_ENTRIES]
        return record

    def _finish_record(self, record: Dict[str, Any], started: float, error: Optional[str] = None) -> None:
        record["durationMs"] = int((time.monotonic() - started) * 1000)
        record["ok"] = error is None
        record["error"] = error
        logger.info(
            "test-exec #%s (%s) %s -> %s",
            record["seq"],
            record["kind"],
            _one_line(record["statement"]),
            "ok" if error is None else f"ERROR {_one_line(error, 120)}",
        )


def _strip_terminator(sql: str) -> str:
    """Drop a trailing `;` or SQL*Plus `/` so EXPLAIN PLAN FOR ... stays valid."""
    text = sql.strip()
    while True:
        if text.endswith(";"):
            text = text[:-1].rstrip()
            continue
        lines = text.split("\n")
        if len(lines) > 1 and lines[-1].strip() == "/":
            text = "\n".join(lines[:-1]).rstrip()
            continue
        return text
