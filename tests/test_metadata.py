"""Tests for oraplanviz_agent.metadata -- the SQL*Plus-to-bind-variable
transformation of the vendored gather_plan_metadata.sql template, and the
Db.fetch_metadata plumbing with a fake driver.
"""

from __future__ import annotations

import pytest

import oraplanviz_agent.db as db_module
from oraplanviz_agent.db import Db, DbError
from oraplanviz_agent.metadata import (
    build_metadata_block,
    get_metadata_block,
    load_template,
)


class TestBuildMetadataBlock:
    def test_block_extracted_and_transformed(self):
        block = build_metadata_block(load_template())

        # Anonymous block, no SQL*Plus plumbing left.
        assert block.startswith("DECLARE")
        assert block.rstrip().endswith("END;")
        assert "&" not in block
        assert "SPOOL" not in block
        assert "DBMS_OUTPUT.PUT_LINE" not in block

        # Substitution literals became binds.
        assert ":arg1" in block
        assert ":arg2" in block
        assert ":bundle := l_buffer;" in block

        # The CLOB must not be freed before the client reads the OUT locator.
        assert "FREETEMPORARY" not in block

        # Bundle skeleton still present (contract markers).
        assert '"format":"ora-plan-metadata"' in block
        assert '"version":2' in block

    def test_substitution_literals_fully_replaced(self):
        template = load_template()
        block = build_metadata_block(template)
        # Every '&arg1'/'&arg2' occurrence in the template's block became a
        # bind; nothing referencing the SQL*Plus arg plumbing survives.
        assert block.count(":arg1") == template.count("'&arg1'")
        assert block.count(":arg2") == template.count("'&arg2'")
        assert "arg3" not in block

    def test_missing_markers_raise(self):
        with pytest.raises(ValueError):
            build_metadata_block("no block here")

    def test_get_metadata_block_cached(self):
        assert get_metadata_block() is get_metadata_block()


class FakeLob:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.executed = None
        self.bind_values = None

    def var(self, _type):
        self._out_var = _FakeVar(self._connection.bundle_text)
        return self._out_var

    def execute(self, statement, **binds):
        if self._connection.raise_on_execute:
            raise self._connection.raise_on_execute
        self.executed = statement
        self.bind_values = binds
        self._connection.last_cursor = self

    def close(self):
        pass


class _FakeVar:
    def __init__(self, text):
        self._text = text

    def getvalue(self):
        if self._text is None:
            return None
        return FakeLob(self._text)


class FakeConnection:
    def __init__(self, bundle_text='{"format":"ora-plan-metadata"}'):
        self.bundle_text = bundle_text
        self.raise_on_execute = None
        self.last_cursor = None

    def cursor(self):
        return FakeCursor(self)


class FakeDriver:
    DB_TYPE_CLOB = object()


@pytest.fixture
def connected_db(monkeypatch):
    monkeypatch.setattr(db_module, "oracledb", FakeDriver())
    db = Db()
    connection = FakeConnection()
    db._connection = connection
    return db, connection


class TestFetchMetadata:
    def test_returns_bundle_text(self, connected_db):
        db, connection = connected_db
        text = db.fetch_metadata("abcd1234efgh5")
        assert text == '{"format":"ora-plan-metadata"}'
        binds = connection.last_cursor.bind_values
        assert binds["arg1"] == "abcd1234efgh5"
        assert binds["arg2"] is None
        assert "bundle" in binds
        assert connection.last_cursor.executed.startswith("DECLARE")

    def test_plan_hash_bound_as_string(self, connected_db):
        db, connection = connected_db
        db.fetch_metadata("abcd1234efgh5", plan_hash=123456789)
        assert connection.last_cursor.bind_values["arg2"] == "123456789"

    def test_not_connected_raises_409(self, monkeypatch):
        monkeypatch.setattr(db_module, "oracledb", FakeDriver())
        db = Db()
        with pytest.raises(DbError) as excinfo:
            db.fetch_metadata("abcd1234efgh5")
        assert excinfo.value.status_code == 409

    def test_driver_error_maps_to_502(self, connected_db):
        db, connection = connected_db
        connection.raise_on_execute = RuntimeError("ORA-01013: user requested cancel")
        with pytest.raises(DbError) as excinfo:
            db.fetch_metadata("abcd1234efgh5")
        assert excinfo.value.status_code == 502
        assert "Metadata gather failed" in excinfo.value.message

    def test_null_bundle_maps_to_500(self, connected_db):
        db, connection = connected_db
        connection.bundle_text = None
        with pytest.raises(DbError) as excinfo:
            db.fetch_metadata("abcd1234efgh5")
        assert excinfo.value.status_code == 500
