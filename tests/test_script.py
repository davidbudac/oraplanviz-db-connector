"""Tests for oraplanviz_agent.script -- pure text handling, no DB involved."""

from __future__ import annotations

from oraplanviz_agent.script import (
    KIND_PLSQL,
    KIND_SKIPPED,
    KIND_SQL,
    Statement,
    feedback_for,
    format_result_set,
    split_statements,
)


def kinds(statements):
    return [(s.kind, s.text) for s in statements]


def test_splits_simple_statements_on_semicolons():
    script = """
    CREATE TABLE t (id NUMBER);
    INSERT INTO t VALUES (1);
    """
    assert kinds(split_statements(script)) == [
        (KIND_SQL, "CREATE TABLE t (id NUMBER)"),
        (KIND_SQL, "INSERT INTO t VALUES (1)"),
    ]


def test_multiline_statement_keeps_its_lines():
    script = "SELECT a,\n       b\n  FROM t;\n"
    statements = split_statements(script)
    assert len(statements) == 1
    assert statements[0].text == "SELECT a,\n       b\n  FROM t"
    assert statements[0].line == 1


def test_trailing_statement_without_semicolon_is_kept():
    assert kinds(split_statements("SELECT 1 FROM dual")) == [(KIND_SQL, "SELECT 1 FROM dual")]


def test_two_statements_on_one_line():
    script = "SELECT 1 FROM dual; SELECT 2 FROM dual;"
    assert kinds(split_statements(script)) == [
        (KIND_SQL, "SELECT 1 FROM dual"),
        (KIND_SQL, "SELECT 2 FROM dual"),
    ]


def test_plsql_block_terminated_by_slash():
    script = """BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(USER, 'T');
END;
/
SELECT 1 FROM dual;
"""
    statements = split_statements(script)
    assert statements[0].kind == KIND_PLSQL
    assert statements[0].text == (
        "BEGIN\n  DBMS_STATS.GATHER_TABLE_STATS(USER, 'T');\nEND;"
    )
    assert kinds(statements)[1] == (KIND_SQL, "SELECT 1 FROM dual")


def test_declare_block_and_create_procedure_are_plsql():
    script = """DECLARE
  n NUMBER;
BEGIN
  n := 1;
END;
/
CREATE OR REPLACE PROCEDURE p AS
BEGIN
  NULL;
END;
/
"""
    statements = split_statements(script)
    assert [s.kind for s in statements] == [KIND_PLSQL, KIND_PLSQL]
    assert statements[1].text.startswith("CREATE OR REPLACE PROCEDURE p AS")
    assert statements[1].text.endswith("END;")


def test_semicolon_inside_a_string_literal_does_not_split():
    script = "INSERT INTO t VALUES ('a;b');\n"
    assert kinds(split_statements(script)) == [(KIND_SQL, "INSERT INTO t VALUES ('a;b')")]


def test_semicolon_inside_comments_does_not_split():
    script = "SELECT 1 -- not a terminator;\n  FROM dual;\n"
    assert kinds(split_statements(script)) == [
        (KIND_SQL, "SELECT 1 -- not a terminator;\n  FROM dual")
    ]


def test_block_comment_spanning_lines_is_transparent():
    script = "SELECT 1 /* a ;\n comment ; */ FROM dual;\n"
    statements = split_statements(script)
    assert len(statements) == 1
    assert statements[0].text.endswith("FROM dual")


def test_optimizer_hint_comment_is_preserved_in_the_statement():
    script = "SELECT /*+ FULL(t) */ * FROM t;\n"
    assert split_statements(script)[0].text == "SELECT /*+ FULL(t) */ * FROM t"


def test_leading_comment_lines_are_dropped_not_executed():
    script = "-- set up\n/* multi\n   line */\nSELECT 1 FROM dual;\n"
    assert kinds(split_statements(script)) == [(KIND_SQL, "SELECT 1 FROM dual")]


def test_sqlplus_directives_are_flagged_not_executed():
    script = "SET SERVEROUTPUT ON\nSPOOL out.log\nSELECT 1 FROM dual;\n"
    statements = split_statements(script)
    assert [s.kind for s in statements] == [KIND_SKIPPED, KIND_SKIPPED, KIND_SQL]
    assert statements[0].text == "SET SERVEROUTPUT ON"


def test_set_transaction_is_real_sql_not_a_directive():
    assert kinds(split_statements("SET TRANSACTION READ ONLY;")) == [
        (KIND_SQL, "SET TRANSACTION READ ONLY")
    ]


def test_exec_shorthand_becomes_a_plsql_block():
    assert kinds(split_statements("EXEC dbms_stats.gather_table_stats(USER, 'T');")) == [
        (KIND_PLSQL, "BEGIN dbms_stats.gather_table_stats(USER, 'T'); END;")
    ]


def test_blank_script_yields_no_statements():
    assert split_statements("\n\n   \n-- nothing\n") == []


def test_quoted_identifier_with_semicolon():
    script = 'SELECT "we;ird" FROM t;\n'
    assert kinds(split_statements(script)) == [(KIND_SQL, 'SELECT "we;ird" FROM t')]


# -- rendering --------------------------------------------------------------


def test_format_result_set_aligns_columns():
    text = format_result_set(["ID", "NAME"], [(1, "alpha"), (22, "b")])
    lines = text.split("\n")
    assert lines[0] == "ID  NAME"
    assert lines[1] == "--  -----"
    assert lines[2] == "1   alpha"
    assert lines[3] == "22  b"
    assert lines[-1] == "2 rows selected."


def test_format_result_set_handles_no_rows_and_nulls():
    assert "no rows selected" in format_result_set(["ID"], [])
    assert format_result_set(["ID"], [(None,)]).split("\n")[2] == ""


def test_format_result_set_marks_truncation():
    text = format_result_set(["ID"], [(1,)], truncated=True)
    assert "output truncated" in text


def test_feedback_for_variants():
    def fb(sql, kind=KIND_SQL, rowcount=None):
        return feedback_for(Statement(sql, kind, 1), rowcount)

    assert fb("BEGIN NULL; END;", KIND_PLSQL) == "PL/SQL procedure successfully completed."
    assert fb("CREATE TABLE t (id NUMBER)") == "Table created."
    assert fb("CREATE UNIQUE INDEX i ON t (id)") == "Index created."
    assert fb("DROP TABLE t") == "Table dropped."
    assert fb("INSERT INTO t VALUES (1)", rowcount=1) == "1 row inserted."
    assert fb("UPDATE t SET id = 2", rowcount=3) == "3 rows updated."
    assert fb("DELETE FROM t", rowcount=0) == "0 rows deleted."
    assert fb("COMMIT") == "Commit complete."
    assert fb("ROLLBACK") == "Rollback complete."
    assert fb("GRANT SELECT ON t TO u") == "Grant succeeded."
    assert fb("ALTER SESSION SET optimizer_mode = ALL_ROWS") == "Session altered."
