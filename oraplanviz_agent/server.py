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
from .db import DbError

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


def create_server(db, token: str, allowed_origins, port: int, host: str = "127.0.0.1"):
    """Build a ThreadingHTTPServer wired to the given Db instance."""

    class _Handler(AgentRequestHandler):
        pass

    _Handler.db = db
    _Handler.token = token
    _Handler.allowed_origins = list(allowed_origins)

    return ThreadingHTTPServer((host, port), _Handler)
