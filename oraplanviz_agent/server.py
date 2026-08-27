"""Stdlib-only HTTP JSON API for the local DB-connect agent.

No web framework: uses http.server.ThreadingHTTPServer + BaseHTTPRequestHandler.
"""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .db import DbError, TestDb

logger = logging.getLogger("oraplanviz_agent.server")

_VALID_RECENT_SOURCES = ("cursor", "monitor")
_VALID_PLAN_SOURCES = ("cursor", "monitor", "awr")
_SQL_ID_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class AgentRequestHandler(BaseHTTPRequestHandler):
    server_version = f"oraplanviz-agent/{__version__}"

    # These are set by create_server() via a subclass / class attrs.
    db = None
    #: The separate test connection (`TestDb`) backing /api/test/*. Never `db`.
    test_db = None
    token: str = ""
    allowed_origins: list = []

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        logger.info("%s - %s", self.address_string(), format % args)

    # -- helpers ----------------------------------------------------------

    def _origin_allowed(self, origin: Optional[str]) -> bool:
        if not origin:
            return False
        if "*" in self.allowed_origins:
            return True
        return origin in self.allowed_origins

    def _apply_cors_headers(self):
        origin = self.headers.get("Origin")
        if self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, status: int, payload: dict):
        body = _json_bytes(payload)
        self.send_response(status)
        self._apply_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str):
        self._send_json(status, {"error": message})

    def _check_auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer ") :]
        return supplied == self.token

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DbError(f"Invalid JSON body: {exc}", 400) from exc

    # -- HTTP verbs ---------------------------------------------------------

    def do_OPTIONS(self):  # noqa: N802 - stdlib naming
        self.send_response(204)
        self._apply_cors_headers()
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/health":
            self._handle_health()
            return

        if not self._require_auth():
            return

        if path == "/api/sql/recent":
            self._handle_recent_sql(query)
        elif path == "/api/plan":
            self._handle_fetch_plan(query)
        elif path == "/api/metadata":
            self._handle_metadata(query)
        elif path == "/api/test/log":
            self._handle_test_log()
        else:
            self._send_error_json(404, "Not found")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._require_auth():
            return

        if path == "/api/connect":
            self._handle_connect()
        elif path == "/api/disconnect":
            self._handle_disconnect()
        elif path == "/api/test/connect":
            self._handle_test_connect()
        elif path == "/api/test/exec":
            self._handle_test_exec()
        elif path == "/api/test/explain":
            self._handle_test_explain()
        elif path == "/api/test/disconnect":
            self._handle_test_disconnect()
        else:
            self._send_error_json(404, "Not found")

    def _require_auth(self) -> bool:
        if not self._check_auth():
            self._send_error_json(401, "Missing or invalid bearer token")
            return False
        return True

    # -- route handlers -----------------------------------------------------

    def _handle_health(self):
        self._send_json(
            200,
            {
                "version": __version__,
                "connected": bool(self.db and self.db.is_connected),
                "oracleVersion": self.db.oracle_version if self.db else None,
                # Whether the separate test connection is open, so the app can
                # tell whether script execution is available at all.
                "testConnected": bool(self.test_db and self.test_db.is_connected),
            },
        )

    def _handle_connect(self):
        try:
            body = self._read_json_body()
            dsn = body.get("dsn")
            user = body.get("user")
            password = body.get("password")
            if not dsn or not user or not password:
                raise DbError("dsn, user, and password are required", 400)
            self.db.connect(dsn, user, password)
            self._send_json(200, {"ok": True, "oracleVersion": self.db.oracle_version})
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)

    def _handle_disconnect(self):
        self.db.disconnect()
        self._send_json(200, {"ok": True})

    def _handle_recent_sql(self, query: dict):
        source = (query.get("source") or ["cursor"])[0]
        if source not in _VALID_RECENT_SOURCES:
            self._send_error_json(
                400, f"Invalid source '{source}'; must be one of {_VALID_RECENT_SOURCES}"
            )
            return
        try:
            items = self.db.recent_sql(source)
            self._send_json(200, {"items": items})
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)

    def _handle_fetch_plan(self, query: dict):
        sql_id = (query.get("sqlId") or [None])[0]
        source = (query.get("source") or ["cursor"])[0]
        child_number_raw = (query.get("childNumber") or ["0"])[0]
        sql_exec_id = (query.get("sqlExecId") or [None])[0]

        if not sql_id or not _SQL_ID_RE.match(sql_id):
            self._send_error_json(400, "Invalid or missing sqlId")
            return
        if source not in _VALID_PLAN_SOURCES:
            self._send_error_json(
                400, f"Invalid source '{source}'; must be one of {_VALID_PLAN_SOURCES}"
            )
            return
        try:
            child_number = int(child_number_raw)
        except ValueError:
            self._send_error_json(400, "Invalid childNumber")
            return

        try:
            text = self.db.fetch_plan(
                sql_id=sql_id,
                source=source,
                child_number=child_number,
                sql_exec_id=sql_exec_id,
            )
            self._send_json(200, {"source": source, "text": text})
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)

    def _handle_metadata(self, query: dict):
        sql_id = (query.get("sqlId") or [None])[0]
        plan_hash_raw = (query.get("planHash") or [None])[0]

        if not sql_id or not _SQL_ID_RE.match(sql_id):
            self._send_error_json(400, "Invalid or missing sqlId")
            return
        plan_hash = None
        if plan_hash_raw is not None:
            try:
                plan_hash = int(plan_hash_raw)
            except ValueError:
                self._send_error_json(400, "Invalid planHash")
                return

        try:
            text = self.db.fetch_metadata(sql_id=sql_id, plan_hash=plan_hash)
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)
            return

        try:
            bundle = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("Metadata gather produced invalid JSON: %s", exc)
            self._send_error_json(500, "Metadata gather produced an invalid bundle")
            return
        self._send_json(200, {"bundle": bundle})

    # -- test connection (approval-gated script execution) ------------------
    #
    # These run on `test_db`, a second Oracle session that is never the
    # read-only source connection above. The UI is responsible for getting an
    # explicit user approval before every /api/test/exec call; the agent's job
    # is to keep the two sessions apart and to log everything it runs.

    def _handle_test_connect(self):
        try:
            body = self._read_json_body()
            dsn = body.get("dsn")
            user = body.get("user")
            password = body.get("password")
            if not dsn or not user or not password:
                raise DbError("dsn, user, and password are required", 400)
            self.test_db.connect(dsn, user, password)
            self._send_json(200, {"ok": True, "oracleVersion": self.test_db.oracle_version})
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)

    def _handle_test_disconnect(self):
        self.test_db.disconnect()
        self._send_json(200, {"ok": True})

    def _handle_test_exec(self):
        try:
            body = self._read_json_body()
            script = body.get("script")
            if not isinstance(script, str) or not script.strip():
                raise DbError("script is required", 400)
            result = self.test_db.exec_script(script)
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)
            return
        # A statement that failed is reported in-band (`ok: false`), not as an
        # HTTP error: the caller wants the transcript either way.
        self._send_json(200, result)

    def _handle_test_explain(self):
        try:
            body = self._read_json_body()
            sql = body.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                raise DbError("sql is required", 400)
            text = self.test_db.explain(sql)
        except DbError as exc:
            self._send_error_json(exc.status_code, exc.message)
            return
        self._send_json(200, {"dbmsXplanText": text})

    def _handle_test_log(self):
        self._send_json(200, {"items": self.test_db.statement_log})


def create_server(db, token: str, allowed_origins, port: int, host: str = "127.0.0.1", test_db=None):
    """Build a ThreadingHTTPServer wired to the given Db instance.

    `test_db` is the separate connection used by /api/test/*; one is created if
    the caller does not supply it, so the two sessions can never collapse into
    the same object by accident.
    """

    class _Handler(AgentRequestHandler):
        pass

    _Handler.db = db
    _Handler.test_db = test_db if test_db is not None else TestDb()
    _Handler.token = token
    _Handler.allowed_origins = list(allowed_origins)

    return ThreadingHTTPServer((host, port), _Handler)
