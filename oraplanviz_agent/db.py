"""Oracle database access layer for the local DB-connect agent.

Uses python-oracledb in *thin* mode (pure Python, no Oracle Instant Client).
The `oracledb` driver is imported lazily on first use so that:
  - the CLI/server modules can be imported (and unit-tested) without the
    driver installed;
  - tests can monkeypatch the module-level `oracledb` attribute with a fake.

All SQL uses bind variables. User-supplied values (sql_id, dsn, user,
password, etc.) are NEVER interpolated into SQL text.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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


class Db:
    """Holds a single Oracle connection for the agent process."""

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
            raise DbError("Not connected to a database", 409)
        return self._connection

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
