"""SQL*Plus-flavoured script splitting and result formatting for the test connection.

`/api/test/exec` accepts a *script* (what an AI proposes and the user approves),
not a single statement, so the agent has to do the little bit of work SQL*Plus
would normally do: split the text into statements, recognise PL/SQL blocks
(terminated by a lone `/`), ignore SQL*Plus-only commands like `SET SERVEROUTPUT
ON`, and render results as a readable transcript.

Everything here is pure text handling -- no driver involvement -- so it is
unit-testable without a database.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Tuple

#: Statement kinds. "skipped" carries a SQL*Plus-only command that the agent
#: acknowledges in the transcript but never sends to the database.
KIND_SQL = "sql"
KIND_PLSQL = "plsql"
KIND_SKIPPED = "skipped"


class Statement(NamedTuple):
    """One statement carved out of a script."""

    text: str
    kind: str
    line: int


# A PL/SQL block starts with DECLARE/BEGIN, or a CREATE of a stored program
# unit. Those are all terminated by a lone "/" rather than by ";", exactly as
# in SQL*Plus.
_PLSQL_OPEN_RE = re.compile(r"^(DECLARE|BEGIN)\b", re.IGNORECASE)
_PLSQL_CREATE_RE = re.compile(
    r"^CREATE\s+(OR\s+REPLACE\s+)?"
    r"((EDITIONABLE|NONEDITIONABLE)\s+)?"
    r"(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE|LIBRARY)\b",
    re.IGNORECASE,
)

# SQL*Plus client commands: harmless to see in a generated script, impossible
# to send to the server. They are reported in the transcript and skipped.
_DIRECTIVE_RE = re.compile(
    r"^(SET|SHOW|SPOOL|PROMPT|REM|REMARK|WHENEVER|COLUMN|COL|DEFINE|UNDEFINE|"
    r"ACCEPT|PAUSE|CLEAR|TTITLE|BTITLE|BREAK|COMPUTE|REPFOOTER|REPHEADER|HOST|"
    r"EXIT|QUIT|CONNECT|DISCONNECT|START|VARIABLE|VAR|PRINT|DESCRIBE|DESC|"
    r"LIST|SAVE|STORE|EDIT|GET|RUN|TIMING|ARCHIVE)\b|^@",
    re.IGNORECASE,
)
# ...except that a few "SET x" forms really are server-side SQL.
_SET_IS_SQL_RE = re.compile(r"^SET\s+(TRANSACTION|ROLE|CONSTRAINTS?)\b", re.IGNORECASE)

# `EXEC foo(1)` is SQL*Plus shorthand; rewrite it into the block it stands for.
_EXEC_RE = re.compile(r"^EXEC(UTE)?\s+(?P<body>\S.*)$", re.IGNORECASE)


def _scan(line: str, state: str) -> Tuple[List[int], str, str]:
    """Scan one physical line, tracking string/comment state across lines.

    Returns ``(semicolon_positions, next_state, code_text)`` where positions are
    indexes of `;` characters that are real statement terminators (not inside a
    literal, a quoted identifier, or a comment) and ``code_text`` is the line
    with comment and literal *content* blanked out -- used only to decide
    whether a line carries any code and what it starts with. The statement text
    handed to the database is always the original, comments included (plan
    hints live in comments).
    """
    positions: List[int] = []
    code = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if state == "block_comment":
            if ch == "*" and i + 1 < n and line[i + 1] == "/":
                state = "code"
                code.append("  ")
                i += 2
                continue
            code.append(" ")
            i += 1
            continue
        if state == "string":
            if ch == "'":
                if i + 1 < n and line[i + 1] == "'":
                    code.append("  ")
                    i += 2
                    continue
                state = "code"
            code.append(" ")
            i += 1
            continue
        if state == "quoted_ident":
            if ch == '"':
                state = "code"
            code.append(" ")
            i += 1
            continue

        # state == "code"
        if ch == "-" and i + 1 < n and line[i + 1] == "-":
            break  # rest of the line is a line comment
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            state = "block_comment"
            code.append("  ")
            i += 2
            continue
        if ch == "'":
            state = "string"
            code.append(" ")
            i += 1
            continue
        if ch == '"':
            state = "quoted_ident"
            code.append(" ")
            i += 1
            continue
        if ch == ";":
            positions.append(i)
        code.append(ch)
        i += 1

    return positions, state, "".join(code)


def _starts_plsql(head: str) -> bool:
    return bool(_PLSQL_OPEN_RE.match(head) or _PLSQL_CREATE_RE.match(head))


def _is_directive(head: str) -> bool:
    if _SET_IS_SQL_RE.match(head):
        return False
    return bool(_DIRECTIVE_RE.match(head))


class _Splitter:
    def __init__(self):
        self.statements: List[Statement] = []
        self._buf: List[str] = []
        self._kind = None
        self._start = 0
        self._state = "code"

    def feed(self, raw: str, lineno: int) -> None:
        segment = raw
        while True:
            positions, next_state, code_text = _scan(segment, self._state)

            if not self._buf:
                head = code_text.strip()
                if not head:
                    # Blank line, or a line that is only a comment.
                    self._state = next_state
                    return
                if _is_directive(head):
                    self.statements.append(
                        Statement(segment.strip(), KIND_SKIPPED, lineno)
                    )
                    self._state = "code"
                    return
                # Detect on the blanked-out code text, but take the body from
                # the original line so string literals survive intact.
                exec_match = _EXEC_RE.match(head) and _EXEC_RE.match(segment.strip())
                if exec_match:
                    body = exec_match.group("body").strip().rstrip(";").strip()
                    self.statements.append(
                        Statement(f"BEGIN {body}; END;", KIND_PLSQL, lineno)
                    )
                    self._state = "code"
                    return
                self._kind = KIND_PLSQL if _starts_plsql(head) else KIND_SQL
                self._start = lineno

            if segment.strip() == "/":
                self._flush()
                self._state = "code"
                return

            if self._kind == KIND_PLSQL or not positions:
                self._buf.append(segment)
                self._state = next_state
                return

            # One or more terminating semicolons on this line.
            pos = positions[0]
            self._buf.append(segment[:pos])
            self._flush()
            segment = segment[pos + 1 :]
            self._state = "code"

    def finish(self) -> List[Statement]:
        self._flush()
        return self.statements

    def _flush(self) -> None:
        text = "\n".join(self._buf).strip()
        kind = self._kind or KIND_SQL
        self._buf = []
        self._kind = None
        if text:
            self.statements.append(Statement(text, kind, self._start))


def split_statements(script: str) -> List[Statement]:
    """Split a SQL*Plus-style script into ordered statements.

    Semicolons terminate ordinary SQL; a lone `/` terminates a PL/SQL block (and
    also flushes a pending SQL statement, as in SQL*Plus). SQL*Plus client
    commands come back as ``KIND_SKIPPED`` entries so the caller can report them
    instead of silently dropping them.
    """
    splitter = _Splitter()
    for lineno, raw in enumerate(script.splitlines(), start=1):
        splitter.feed(raw.rstrip("\r"), lineno)
    return splitter.finish()


# -- result rendering -------------------------------------------------------

_MAX_CELL_CHARS = 120

_DDL_PAST_TENSE = {
    "CREATE": "created",
    "DROP": "dropped",
    "ALTER": "altered",
    "TRUNCATE": "truncated",
    "RENAME": "renamed",
    "ANALYZE": "analyzed",
    "COMMENT": "created",
}
_DML_VERBS = {
    "INSERT": "inserted",
    "UPDATE": "updated",
    "DELETE": "deleted",
    "MERGE": "merged",
}


def _cell(value) -> str:
    if value is None:
        return ""
    read = getattr(value, "read", None)
    if callable(read):  # LOB
        try:
            value = read()
        except Exception:  # noqa: BLE001 - best-effort rendering
            return "<lob>"
    text = str(value)
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if len(text) > _MAX_CELL_CHARS:
        text = text[: _MAX_CELL_CHARS - 3] + "..."
    return text


def format_result_set(columns: List[str], rows: List[tuple], truncated: bool = False) -> str:
    """Render a fetched result set as a small fixed-width text table."""
    headers = [str(c) for c in columns]
    body = [[_cell(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def _line(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [_line(headers), _line(["-" * w for w in widths])]
    lines.extend(_line(row) for row in body)
    if not body:
        lines.append("no rows selected")
    else:
        count = len(body)
        lines.append("")
        lines.append(
            f"{count} row{'s' if count != 1 else ''} selected."
            + (" (output truncated)" if truncated else "")
        )
    return "\n".join(lines)


def feedback_for(statement: Statement, rowcount=None) -> str:
    """SQL*Plus-style one-line confirmation for a non-query statement."""
    if statement.kind == KIND_PLSQL:
        return "PL/SQL procedure successfully completed."

    tokens = re.findall(r"[A-Za-z_$#]+", statement.text.upper())
    verb = tokens[0] if tokens else ""

    if verb in _DML_VERBS:
        n = rowcount if isinstance(rowcount, int) and rowcount >= 0 else 0
        return f"{n} row{'s' if n != 1 else ''} {_DML_VERBS[verb]}."
    if verb == "COMMIT":
        return "Commit complete."
    if verb == "ROLLBACK":
        return "Rollback complete."
    if verb in ("GRANT", "REVOKE"):
        return f"{verb.capitalize()} succeeded."
    if verb in _DDL_PAST_TENSE:
        obj = tokens[1] if len(tokens) > 1 else ""
        if obj in ("OR", "GLOBAL", "UNIQUE", "BITMAP", "PUBLIC", "MATERIALIZED"):
            obj = next(
                (
                    t
                    for t in tokens[1:]
                    if t
                    in (
                        "TABLE",
                        "INDEX",
                        "VIEW",
                        "SEQUENCE",
                        "SYNONYM",
                        "TRIGGER",
                        "PROCEDURE",
                        "FUNCTION",
                        "PACKAGE",
                        "TYPE",
                        "USER",
                        "ROLE",
                    )
                ),
                obj,
            )
        if obj:
            return f"{obj.capitalize()} {_DDL_PAST_TENSE[verb]}."
    return "Statement executed."
