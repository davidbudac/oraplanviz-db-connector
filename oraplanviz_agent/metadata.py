"""Metadata bundle gathering for the local DB-connect agent.

Rather than reimplementing the ~1700-line gather_plan_metadata.sql PL/SQL
logic in Python (and inevitably drifting from the frontend's JSON contract),
the canonical script from the visualizer repo is vendored verbatim as package
data (gather_plan_metadata.sql, upstream:
ora_explain_plan_viz/scripts/gather_plan_metadata.sql).

At runtime this module extracts the script's anonymous PL/SQL block and
adapts it from SQL*Plus to python-oracledb:

  - the SQL*Plus substitution literals '&arg1' / '&arg2' become real bind
    variables :arg1 / :arg2 (the block compares/assigns them as strings, so
    a plain VARCHAR2 bind is a drop-in replacement);
  - the final emit section (which chunks the CLOB buffer through
    DBMS_OUTPUT.PUT_LINE for spool capture) is replaced with a single
    `:bundle := l_buffer;` OUT-bind assignment, so the driver reads the
    bundle JSON directly from the CLOB with no line-chunking round trip.

The resulting block produces byte-identical bundle JSON to the SQL*Plus
script (format "ora-plan-metadata", version 2).
"""

from __future__ import annotations

from importlib import resources
from typing import Optional

_TEMPLATE_NAME = "gather_plan_metadata.sql"

_block_cache: Optional[str] = None


def load_template() -> str:
    """Read the vendored gather_plan_metadata.sql template."""
    return (
        resources.files("oraplanviz_agent")
        .joinpath(_TEMPLATE_NAME)
        .read_text(encoding="utf-8")
    )


def build_metadata_block(template: str) -> str:
    """Turn the SQL*Plus gather script into a bindable anonymous PL/SQL block.

    Binds: :arg1 (sql_id, or the literal 'LIST'), :arg2 (plan_hash_value or
    object list; NULL allowed), :bundle (OUT CLOB receiving the JSON).
    """
    # The block spans from the only column-0 DECLARE to the column-0 "END;"
    # that is immediately followed by the SQL*Plus "/" terminator. Inner END;
    # lines are all indented, so these anchors are unambiguous.
    try:
        start = template.index("\nDECLARE\n") + 1
        end = template.index("\nEND;\n/") + len("\nEND;")
    except ValueError as exc:
        raise ValueError(
            "gather_plan_metadata.sql template does not contain the expected "
            "DECLARE ... END; / anonymous block"
        ) from exc
    block = template[start:end]

    block = block.replace("'&arg1'", ":arg1").replace("'&arg2'", ":arg2")

    lines = block.split("\n")
    emit_idx = _line_index(lines, "-- Emit")
    free_idx = _line_index(lines, "DBMS_LOB.FREETEMPORARY(l_buffer);")
    if free_idx < emit_idx:
        raise ValueError("Emit section of the gather template is malformed")
    # emit_idx - 1 is the dashed separator above "-- Emit". Everything through
    # FREETEMPORARY (the DBMS_OUTPUT chunk loop included) is replaced by the
    # OUT-bind assignment. The temporary CLOB must stay allocated: the client
    # reads it through the returned locator after the block completes.
    lines[emit_idx - 1 : free_idx + 1] = ["  :bundle := l_buffer;"]
    block = "\n".join(lines)

    if "&" in block:
        raise ValueError(
            "Unexpected SQL*Plus substitution variable left in the gather block"
        )
    return block


def _line_index(lines, stripped_value: str) -> int:
    for i, line in enumerate(lines):
        if line.strip() == stripped_value:
            return i
    raise ValueError(
        f"gather_plan_metadata.sql template is missing expected line: {stripped_value!r}"
    )


def get_metadata_block() -> str:
    """Cached accessor for the transformed PL/SQL block."""
    global _block_cache
    if _block_cache is None:
        _block_cache = build_metadata_block(load_template())
    return _block_cache
