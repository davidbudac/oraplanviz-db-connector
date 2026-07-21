--------------------------------------------------------------------------------
-- gather_plan_metadata.sql
--
-- Emit a `format: "ora-plan-metadata"` JSON bundle for the objects referenced
-- by a specific SQL_ID/PLAN_HASH_VALUE, or for an explicit list of objects.
--
-- The bundle is consumed by the Oracle Execution Plan Visualizer to annotate
-- plan nodes with table, column, and index statistics from the data dictionary,
-- plus partitioning method/keys and a simplified CREATE DDL per object.
--
-- Usage (SQL*Plus or SQLcl, Oracle 12.2+):
--
--   SQL> @gather_plan_metadata.sql <sql_id> [<plan_hash_value>] [<spool_file>]
--   SQL> @gather_plan_metadata.sql LIST "HR.EMPLOYEES,HR.DEPARTMENTS" [<spool_file>]
--
--   The first form gathers metadata for all objects referenced by the SQL_ID
--   (looks in V$SQL_PLAN first, then DBA_HIST_SQL_PLAN). The literal LIST
--   keyword switches to manual-object-list mode.
--
--   The script spools its own output: the bundle is written to <spool_file>
--   (default bundle.json in the current directory). No manual SPOOL needed.
--
-- Privileges:
--   The script tries DBA_* views first and degrades to ALL_* on ORA-00942.
--   Objects skipped because of insufficient privileges land in the bundle's
--   `coverage_warnings` array. Run as a DBA for full coverage.
--
--   Bundle version 2 additionally reads (all optional, each degrading to a
--   coverage warning or null field rather than failing the gather):
--   DBA_TAB_COLS, DBA_CONSTRAINTS/DBA_CONS_COLUMNS, DBA_SEGMENTS (falls back
--   to USER_SEGMENTS for the connected schema's own objects), DBA_TABLES
--   (physical attributes), DBA_STAT_EXTENSIONS, and - in SQL_ID mode only -
--   V$SQL, V$SQL_OPTIMIZER_ENV, DBA_SQL_PLAN_BASELINES, DBA_SQL_PROFILES,
--   DBA_SQL_PATCHES, DBA_SQL_PLAN_DIRECTIVES and DBA_SQL_PLAN_DIR_OBJECTS.
--
-- This script is read-only against the data dictionary.
--------------------------------------------------------------------------------

SET ECHO OFF
SET HEADING OFF
SET FEEDBACK OFF
SET PAGESIZE 0
SET LINESIZE 32767
SET LONG 100000000
SET LONGCHUNKSIZE 100000
SET SERVEROUTPUT ON SIZE UNLIMITED FORMAT WRAPPED
SET TRIMSPOOL ON
SET TRIMOUT ON
SET TAB OFF
SET TERMOUT ON
SET VERIFY OFF

-- The @@GEN:...@@ marker comments delimit the sections the visualizer's
-- in-app generator swaps out when it stamps a self-contained copy of this
-- script (literal arguments, screen instead of spool output). They are
-- plain comments - running this file directly ignores them.

-- Positional arguments: SQL_ID or LIST, then either plan_hash/spool or
-- object_list/spool. Args 2 and 3 are optional - default them to empty via
-- the zero-row NEW_VALUE idiom so SQL*Plus never prompts for them.
-- @@GEN:ARGS:BEGIN@@
SET TERMOUT OFF
COLUMN 2 NEW_VALUE 2 NOPRINT
COLUMN 3 NEW_VALUE 3 NOPRINT
SELECT NULL "2", NULL "3" FROM dual WHERE 1 = 2;
SET TERMOUT ON

DEFINE arg1 = "&1"
DEFINE arg2 = "&2"
DEFINE arg3 = "&3"

-- Spool target: optional third argument, default bundle.json
SET TERMOUT OFF
COLUMN spool_target NEW_VALUE spool_target NOPRINT
SELECT NVL(TRIM('&arg3'), 'bundle.json') AS spool_target FROM dual;
SET TERMOUT ON
-- @@GEN:ARGS:END@@

-- @@GEN:OPEN:BEGIN@@
PROMPT Gathering plan metadata into &spool_target ...

-- TERMOUT OFF keeps the JSON off the screen; it still reaches the spool file.
SET TERMOUT OFF
SPOOL &spool_target
-- @@GEN:OPEN:END@@

DECLARE
  g_mode            VARCHAR2(16);             -- 'SQL_ID' or 'LIST'
  g_sql_id          VARCHAR2(32) := NULL;
  g_plan_hash       NUMBER := NULL;
  g_manual_list     VARCHAR2(4000) := NULL;

  g_container       VARCHAR2(128);
  g_db_name         VARCHAR2(128);
  g_oracle_version  VARCHAR2(128);

  g_use_dba_tables  BOOLEAN := TRUE;
  g_use_dba_indexes BOOLEAN := TRUE;
  g_use_dba_cols    BOOLEAN := TRUE;
  g_use_dba_constr  BOOLEAN := TRUE;
  g_use_dba_part    BOOLEAN := TRUE;
  g_use_dba_seg     BOOLEAN := TRUE;   -- dba_segments -> user_segments (own schema only)
  g_use_dba_ext     BOOLEAN := TRUE;   -- dba_stat_extensions

  -- One-shot: only warn once about missing DBA_SEGMENTS access, even though
  -- write_segment is called once per table and once per index.
  g_seg_warned      BOOLEAN := FALSE;

  TYPE t_object_rec IS RECORD (
    owner     VARCHAR2(128),
    name      VARCHAR2(128),
    type      VARCHAR2(64)
  );
  TYPE t_object_tab IS TABLE OF t_object_rec INDEX BY PLS_INTEGER;
  l_objects t_object_tab;

  TYPE t_warning_rec IS RECORD (
    object VARCHAR2(257),
    reason VARCHAR2(4000)
  );
  TYPE t_warning_tab IS TABLE OF t_warning_rec INDEX BY PLS_INTEGER;
  l_warnings t_warning_tab;

  l_warn_count PLS_INTEGER := 0;

  -- Output buffer. Declared here because PL/SQL requires item declarations
  -- to precede subprogram bodies in a declarative part.
  l_buffer CLOB;

  PROCEDURE add_warning(p_object IN VARCHAR2, p_reason IN VARCHAR2) IS
  BEGIN
    l_warn_count := l_warn_count + 1;
    l_warnings(l_warn_count).object := SUBSTR(p_object, 1, 257);
    l_warnings(l_warn_count).reason := SUBSTR(p_reason, 1, 4000);
  END;

  -- Escape a string for embedding *inside* JSON double quotes (no surrounding
  -- quotes). Shared by js_string and the CLOB DDL emitter, which escapes the
  -- DDL one bounded chunk at a time.
  FUNCTION js_escape(p_value IN VARCHAR2) RETURN VARCHAR2 IS
    l_out VARCHAR2(32767);
  BEGIN
    l_out := p_value;
    l_out := REPLACE(l_out, '\', '\\');
    l_out := REPLACE(l_out, '"', '\"');
    l_out := REPLACE(l_out, CHR(10), '\n');
    l_out := REPLACE(l_out, CHR(13), '\r');
    l_out := REPLACE(l_out, CHR(9), '\t');
    RETURN l_out;
  END;

  -- JSON-safe quoting for an arbitrary string value.
  FUNCTION js_string(p_value IN VARCHAR2) RETURN VARCHAR2 IS
  BEGIN
    IF p_value IS NULL THEN
      RETURN 'null';
    END IF;
    RETURN '"' || js_escape(p_value) || '"';
  END;

  FUNCTION js_number(p_value IN NUMBER) RETURN VARCHAR2 IS
    l_out VARCHAR2(64);
  BEGIN
    IF p_value IS NULL THEN RETURN 'null'; END IF;
    l_out := TO_CHAR(p_value, 'TM', 'NLS_NUMERIC_CHARACTERS=''.,''');
    -- TM format drops the leading zero (.5 / -.5), which is invalid JSON.
    IF SUBSTR(l_out, 1, 1) = '.' THEN
      l_out := '0' || l_out;
    ELSIF SUBSTR(l_out, 1, 2) = '-.' THEN
      l_out := '-0' || SUBSTR(l_out, 2);
    END IF;
    RETURN l_out;
  END;

  FUNCTION js_bool(p_value IN BOOLEAN) RETURN VARCHAR2 IS
  BEGIN
    IF p_value IS NULL THEN RETURN 'null'; END IF;
    RETURN CASE WHEN p_value THEN 'true' ELSE 'false' END;
  END;

  FUNCTION js_iso_ts(p_value IN DATE) RETURN VARCHAR2 IS
  BEGIN
    IF p_value IS NULL THEN RETURN 'null'; END IF;
    RETURN '"' || TO_CHAR(p_value, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') || '"';
  END;

  FUNCTION js_iso_ts(p_value IN TIMESTAMP) RETURN VARCHAR2 IS
  BEGIN
    IF p_value IS NULL THEN RETURN 'null'; END IF;
    RETURN '"' || TO_CHAR(p_value, 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"') || '"';
  END;

  -- Fetch a simplified CREATE DDL for one object via DBMS_METADATA. Transform
  -- params (set once in the main block) strip storage/segment/tablespace noise
  -- and pretty-print. DBMS_METADATA respects privileges: a user can always get
  -- DDL for its own objects, but needs SELECT_CATALOG_ROLE (or explicit grants)
  -- for others, in which case GET_DDL raises ORA-31603 - downgrade that to a
  -- coverage warning and emit a null ddl rather than failing the whole gather.
  FUNCTION get_object_ddl(
    p_md_type IN VARCHAR2,   -- DBMS_METADATA object type, e.g. 'TABLE' or 'INDEX'
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2
  ) RETURN CLOB IS
    l_ddl CLOB;
  BEGIN
    -- Dynamic call so the block still COMPILES in a hardened DB where EXECUTE
    -- on DBMS_METADATA has been revoked from PUBLIC; there it degrades to a
    -- runtime error caught below rather than an ORA-06550 that kills the gather.
    EXECUTE IMMEDIATE 'BEGIN :ddl := DBMS_METADATA.GET_DDL(:t, :n, :o); END;'
      USING OUT l_ddl, IN p_md_type, IN p_name, IN p_owner;
    RETURN l_ddl;
  EXCEPTION
    WHEN OTHERS THEN
      add_warning(p_owner || '.' || p_name,
        'Simplified DDL not captured (needs SELECT_CATALOG_ROLE or object ownership): '
        || SUBSTR(SQLERRM, 1, 200));
      RETURN NULL;
  END;

  -- Append a "ddl":<json-string|null> field, escaping the DDL CLOB one bounded
  -- chunk at a time so it never overflows the 32767-char VARCHAR2 limit that a
  -- whole-CLOB js_string() call would hit on a wide table.
  PROCEDURE append_ddl_field(p_buffer IN OUT NOCOPY CLOB, p_ddl IN CLOB) IS
    l_len   INTEGER;
    l_off   INTEGER := 1;
    l_amt   INTEGER;
    l_chunk VARCHAR2(32767);
  BEGIN
    IF p_ddl IS NULL THEN
      DBMS_LOB.APPEND(p_buffer, '"ddl":null');
      RETURN;
    END IF;
    DBMS_LOB.APPEND(p_buffer, '"ddl":"');
    l_len := DBMS_LOB.GETLENGTH(p_ddl);
    WHILE l_off <= l_len LOOP
      l_amt   := LEAST(8000, l_len - l_off + 1);
      l_chunk := DBMS_LOB.SUBSTR(p_ddl, l_amt, l_off);
      DBMS_LOB.APPEND(p_buffer, js_escape(l_chunk));
      l_off   := l_off + l_amt;
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, '"');
  END;

  -- Partition (or subpartition) key columns of a table or index, rendered as a
  -- JSON array string like ["SALE_DATE","REGION"]. Best-effort: any error
  -- (including ORA-00942 on the *_PART_KEY_COLUMNS view) yields [].
  FUNCTION js_part_key_cols(
    p_owner       IN VARCHAR2,
    p_name        IN VARCHAR2,
    p_object_type IN VARCHAR2,   -- 'TABLE' or 'INDEX'
    p_subpart     IN BOOLEAN,
    p_use_dba     IN BOOLEAN
  ) RETURN VARCHAR2 IS
    l_view  VARCHAR2(64);
    l_cols  SYS.ODCIVARCHAR2LIST;
    l_out   VARCHAR2(4000);
    l_first BOOLEAN := TRUE;
  BEGIN
    IF p_subpart THEN
      l_view := CASE WHEN p_use_dba THEN 'dba_subpart_key_columns' ELSE 'all_subpart_key_columns' END;
    ELSE
      l_view := CASE WHEN p_use_dba THEN 'dba_part_key_columns' ELSE 'all_part_key_columns' END;
    END IF;
    EXECUTE IMMEDIATE
      'SELECT column_name FROM ' || l_view
      || ' WHERE owner = :1 AND name = :2 AND object_type = :3 ORDER BY column_position'
      BULK COLLECT INTO l_cols USING p_owner, p_name, p_object_type;
    l_out := '[';
    FOR i IN 1 .. l_cols.COUNT LOOP
      IF NOT l_first THEN l_out := l_out || ','; END IF;
      l_first := FALSE;
      l_out := l_out || js_string(l_cols(i));
    END LOOP;
    RETURN l_out || ']';
  EXCEPTION
    WHEN OTHERS THEN
      RETURN '[]';
  END;

  -- Try the DBA_/ALL_ pair. Returns TRUE if DBA_ succeeded, FALSE if it fell
  -- back to ALL_. Raises any error other than ORA-00942.
  FUNCTION probe_dba_view(p_view IN VARCHAR2) RETURN BOOLEAN IS
    l_dummy NUMBER;
  BEGIN
    EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM ' || p_view || ' WHERE ROWNUM = 1'
      INTO l_dummy;
    RETURN TRUE;
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE = -942 THEN
        RETURN FALSE;
      END IF;
      RAISE;
  END;

  -- Decode a RAW low_value/high_value to its native textual representation.
  FUNCTION decode_raw_value(
    p_raw       IN RAW,
    p_data_type IN VARCHAR2
  ) RETURN VARCHAR2 IS
    l_num NUMBER;
    l_vc  VARCHAR2(4000);
    l_dt  DATE;
  BEGIN
    IF p_raw IS NULL THEN RETURN NULL; END IF;
    IF UPPER(p_data_type) LIKE 'NUMBER%'
       OR UPPER(p_data_type) IN ('FLOAT', 'BINARY_FLOAT', 'BINARY_DOUBLE') THEN
      DBMS_STATS.CONVERT_RAW_VALUE(p_raw, l_num);
      RETURN TO_CHAR(l_num, 'TM', 'NLS_NUMERIC_CHARACTERS=''.,''');
    ELSIF UPPER(p_data_type) IN ('CHAR', 'VARCHAR2', 'NCHAR', 'NVARCHAR2') THEN
      DBMS_STATS.CONVERT_RAW_VALUE(p_raw, l_vc);
      RETURN l_vc;
    ELSIF UPPER(p_data_type) = 'DATE' THEN
      DBMS_STATS.CONVERT_RAW_VALUE(p_raw, l_dt);
      RETURN TO_CHAR(l_dt, 'YYYY-MM-DD"T"HH24:MI:SS');
    ELSIF UPPER(p_data_type) LIKE 'TIMESTAMP%' THEN
      -- DBMS_STATS.CONVERT_RAW_VALUE has no TIMESTAMP overload. The first
      -- 7 bytes of a timestamp raw use the DATE encoding, so decode those
      -- (fractional seconds are not needed for low/high display).
      DBMS_STATS.CONVERT_RAW_VALUE(UTL_RAW.SUBSTR(p_raw, 1, 7), l_dt);
      RETURN TO_CHAR(l_dt, 'YYYY-MM-DD"T"HH24:MI:SS');
    END IF;
    RETURN RAWTOHEX(p_raw);
  EXCEPTION
    WHEN OTHERS THEN
      RETURN RAWTOHEX(p_raw);
  END;

  PROCEDURE add_object(p_owner IN VARCHAR2, p_name IN VARCHAR2, p_type IN VARCHAR2) IS
    l_idx PLS_INTEGER := l_objects.COUNT + 1;
    l_dup BOOLEAN := FALSE;
  BEGIN
    -- Dedup
    FOR i IN 1 .. l_objects.COUNT LOOP
      IF l_objects(i).owner = p_owner AND l_objects(i).name = p_name THEN
        l_dup := TRUE;
        EXIT;
      END IF;
    END LOOP;
    IF NOT l_dup THEN
      l_objects(l_idx).owner := p_owner;
      l_objects(l_idx).name := p_name;
      l_objects(l_idx).type := p_type;
    END IF;
  END;

  -- Resolve referenced objects from a plan-source view (V$SQL_PLAN or
  -- DBA_HIST_SQL_PLAN). Uses native dynamic SQL on purpose: a plain schema
  -- owner has no SELECT on these views, and a *static* reference would make
  -- the whole anonymous block fail to COMPILE (ORA-06550) before any
  -- EXCEPTION handler can run. Dynamic SQL turns the missing view into a
  -- catchable runtime ORA-00942, which we downgrade to a coverage warning.
  PROCEDURE resolve_from_plan_view(
    p_view     IN VARCHAR2,
    p_warn_obj IN VARCHAR2,
    p_warn_msg IN VARCHAR2
  ) IS
    l_cur   SYS_REFCURSOR;
    l_owner VARCHAR2(128);
    l_name  VARCHAR2(128);
    l_type  VARCHAR2(128);
    l_sql   VARCHAR2(1000);
  BEGIN
    l_sql := 'SELECT DISTINCT object_owner, object_name, object_type '
          || 'FROM ' || p_view || ' '
          || 'WHERE sql_id = :a '
          || 'AND (:b IS NULL OR plan_hash_value = :c) '
          || 'AND object_owner IS NOT NULL AND object_name IS NOT NULL';
    OPEN l_cur FOR l_sql USING g_sql_id, g_plan_hash, g_plan_hash;
    LOOP
      FETCH l_cur INTO l_owner, l_name, l_type;
      EXIT WHEN l_cur%NOTFOUND;
      add_object(l_owner, l_name, l_type);
    END LOOP;
    CLOSE l_cur;
  EXCEPTION
    WHEN OTHERS THEN
      IF l_cur%ISOPEN THEN CLOSE l_cur; END IF;
      IF SQLCODE = -942 THEN
        add_warning(p_warn_obj, p_warn_msg);
      ELSE
        RAISE;
      END IF;
  END;

  PROCEDURE resolve_objects_for_sql_id IS
  BEGIN
    resolve_from_plan_view('v$sql_plan', 'V$SQL_PLAN',
      'No SELECT on V$SQL_PLAN - cannot resolve referenced objects from cursor cache. '
      || 'Re-run as a user with SELECT_CATALOG_ROLE, or use LIST mode with explicit OWNER.OBJECT names.');

    IF l_objects.COUNT = 0 THEN
      resolve_from_plan_view('dba_hist_sql_plan', 'DBA_HIST_SQL_PLAN',
        'No SELECT on DBA_HIST_SQL_PLAN - cannot resolve referenced objects from AWR history.');
    END IF;
  END;

  PROCEDURE resolve_objects_from_list IS
    l_remaining VARCHAR2(4000) := g_manual_list;
    l_token     VARCHAR2(257);
    l_pos       PLS_INTEGER;
    l_dot       PLS_INTEGER;
  BEGIN
    WHILE l_remaining IS NOT NULL LOOP
      l_pos := INSTR(l_remaining, ',');
      IF l_pos > 0 THEN
        l_token := TRIM(SUBSTR(l_remaining, 1, l_pos - 1));
        l_remaining := SUBSTR(l_remaining, l_pos + 1);
      ELSE
        l_token := TRIM(l_remaining);
        l_remaining := NULL;
      END IF;
      IF l_token IS NOT NULL THEN
        l_dot := INSTR(l_token, '.');
        IF l_dot > 0 THEN
          add_object(
            UPPER(SUBSTR(l_token, 1, l_dot - 1)),
            UPPER(SUBSTR(l_token, l_dot + 1)),
            'UNKNOWN'
          );
        ELSE
          add_warning(l_token, 'Manual list entry has no owner - expected OWNER.OBJECT.');
        END IF;
      END IF;
    END LOOP;
  END;

  PROCEDURE write_table_stats(
    p_owner    IN VARCHAR2,
    p_name     IN VARCHAR2,
    p_buffer   IN OUT NOCOPY CLOB
  ) IS
    l_num_rows      NUMBER;
    l_blocks        NUMBER;
    l_avg_row_len   NUMBER;
    l_last_analyzed DATE;
    l_stale         VARCHAR2(3);
    l_partitioned   VARCHAR2(3);
    l_partition_cnt NUMBER;
    l_part_type     VARCHAR2(32);
    l_subpart_type  VARCHAR2(32);
    l_interval      VARCHAR2(1000);
    l_view          VARCHAR2(64);
    l_part_view     VARCHAR2(64);
    l_stmt          VARCHAR2(1000);
  BEGIN
    l_view := CASE WHEN g_use_dba_tables THEN 'dba_tab_statistics' ELSE 'all_tab_statistics' END;
    l_stmt := 'SELECT num_rows, blocks, avg_row_len, last_analyzed, stale_stats '
           || 'FROM ' || l_view || ' '
           || 'WHERE owner = :1 AND table_name = :2 AND object_type = ''TABLE''';
    BEGIN
      EXECUTE IMMEDIATE l_stmt
        INTO l_num_rows, l_blocks, l_avg_row_len, l_last_analyzed, l_stale
        USING p_owner, p_name;
    EXCEPTION
      WHEN NO_DATA_FOUND THEN
        add_warning(p_owner || '.' || p_name, 'No row in ' || l_view || ' - table stats not gathered.');
      WHEN OTHERS THEN
        IF SQLCODE = -942 AND g_use_dba_tables THEN
          g_use_dba_tables := FALSE;
          write_table_stats(p_owner, p_name, p_buffer);
          RETURN;
        END IF;
        add_warning(p_owner || '.' || p_name, 'Table stats query failed: ' || SQLERRM);
    END;

    -- Partitioned flag and count
    BEGIN
      l_view := CASE WHEN g_use_dba_tables THEN 'dba_tables' ELSE 'all_tables' END;
      EXECUTE IMMEDIATE
        'SELECT partitioned FROM ' || l_view || ' WHERE owner = :1 AND table_name = :2'
        INTO l_partitioned USING p_owner, p_name;
      IF l_partitioned = 'YES' THEN
        l_part_view := CASE WHEN g_use_dba_part THEN 'dba_tab_partitions' ELSE 'all_tab_partitions' END;
        BEGIN
          EXECUTE IMMEDIATE
            'SELECT COUNT(*) FROM ' || l_part_view || ' WHERE table_owner = :1 AND table_name = :2'
            INTO l_partition_cnt USING p_owner, p_name;
        EXCEPTION
          WHEN OTHERS THEN
            IF SQLCODE = -942 AND g_use_dba_part THEN
              g_use_dba_part := FALSE;
              l_part_view := 'all_tab_partitions';
              BEGIN
                EXECUTE IMMEDIATE
                  'SELECT COUNT(*) FROM ' || l_part_view || ' WHERE table_owner = :1 AND table_name = :2'
                  INTO l_partition_cnt USING p_owner, p_name;
              EXCEPTION WHEN OTHERS THEN l_partition_cnt := NULL;
              END;
            ELSE
              l_partition_cnt := NULL;
            END IF;
        END;
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        l_partitioned := NULL;
    END;

    -- Partitioning method + interval expression (only meaningful when the
    -- table is partitioned). Try the DBA_ view, fall back to ALL_ on any error.
    IF l_partitioned = 'YES' THEN
      BEGIN
        EXECUTE IMMEDIATE
          'SELECT partitioning_type, subpartitioning_type, interval FROM '
          || CASE WHEN g_use_dba_part THEN 'dba_part_tables' ELSE 'all_part_tables' END
          || ' WHERE owner = :1 AND table_name = :2'
          INTO l_part_type, l_subpart_type, l_interval USING p_owner, p_name;
      EXCEPTION
        WHEN OTHERS THEN
          BEGIN
            EXECUTE IMMEDIATE
              'SELECT partitioning_type, subpartitioning_type, interval FROM all_part_tables'
              || ' WHERE owner = :1 AND table_name = :2'
              INTO l_part_type, l_subpart_type, l_interval USING p_owner, p_name;
          EXCEPTION
            WHEN OTHERS THEN
              l_part_type := NULL; l_subpart_type := NULL; l_interval := NULL;
          END;
      END;
    END IF;

    DBMS_LOB.APPEND(p_buffer,
      '"stats":{'
      || '"num_rows":' || js_number(l_num_rows)
      || ',"blocks":' || js_number(l_blocks)
      || ',"avg_row_len":' || js_number(l_avg_row_len)
      || ',"last_analyzed":' || js_iso_ts(l_last_analyzed)
      || ',"stale_stats":' || CASE WHEN l_stale IS NULL THEN 'null' ELSE js_string(l_stale) END
      || ',"partitioned":' || js_bool(l_partitioned = 'YES')
      || CASE WHEN l_partition_cnt IS NOT NULL THEN ',"partition_count":' || js_number(l_partition_cnt) ELSE '' END
      || CASE WHEN l_partitioned = 'YES' THEN
             ',"partition_type":' || js_string(l_part_type)
             || ',"subpartition_type":' || js_string(l_subpart_type)
             || ',"interval":' || js_string(TRIM(l_interval))
             || ',"partition_key":' || js_part_key_cols(p_owner, p_name, 'TABLE', FALSE, g_use_dba_part)
             || CASE WHEN l_subpart_type IS NOT NULL AND l_subpart_type <> 'NONE'
                     THEN ',"subpartition_key":' || js_part_key_cols(p_owner, p_name, 'TABLE', TRUE, g_use_dba_part)
                     ELSE '' END
           ELSE '' END
      || '}'
    );
  END;

  PROCEDURE write_columns(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_view    VARCHAR2(64);
    l_first   BOOLEAN := TRUE;
    l_stmt    VARCHAR2(2000);
    TYPE t_col IS RECORD (
      col_name      VARCHAR2(128),
      data_type     VARCHAR2(128),
      nullable      VARCHAR2(1),
      num_distinct  NUMBER,
      num_nulls     NUMBER,
      low_value     RAW(2000),
      high_value    RAW(2000),
      density       NUMBER,
      histogram     VARCHAR2(32),
      hist_buckets  NUMBER,
      virtual_col   VARCHAR2(3),
      hidden_col    VARCHAR2(3)
    );
    TYPE t_col_tab IS TABLE OF t_col;
    l_cols    t_col_tab;
    l_low_dec VARCHAR2(4000);
    l_high_dec VARCHAR2(4000);
  BEGIN
    -- {DBA|ALL}_TAB_COLS is a superset of {DBA|ALL}_TAB_COLUMNS that also
    -- carries hidden/virtual columns (e.g. the SYS_STUxxxxx columns backing
    -- extended stats column groups), plus INTERNAL_COLUMN_ID for ordering.
    l_view := CASE WHEN g_use_dba_cols THEN 'dba_tab_cols' ELSE 'all_tab_cols' END;
    l_stmt := 'SELECT c.column_name, c.data_type, c.nullable, '
           || '       c.num_distinct, c.num_nulls, c.low_value, c.high_value, c.density, '
           || '       NVL(c.histogram, ''NONE''), NVL(c.num_buckets, 0), '
           || '       c.virtual_column, c.hidden_column '
           || 'FROM ' || l_view || ' c '
           || 'WHERE c.owner = :1 AND c.table_name = :2 '
           || 'ORDER BY CASE WHEN c.hidden_column = ''YES'' THEN 1 ELSE 0 END, '
           || '         NVL(c.column_id, 100000), c.internal_column_id';

    BEGIN
      EXECUTE IMMEDIATE l_stmt BULK COLLECT INTO l_cols USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE = -942 AND g_use_dba_cols THEN
          g_use_dba_cols := FALSE;
          write_columns(p_owner, p_name, p_buffer);
          RETURN;
        END IF;
        add_warning(p_owner || '.' || p_name, 'Column stats query failed: ' || SQLERRM);
        DBMS_LOB.APPEND(p_buffer, '"columns":{}');
        RETURN;
    END;

    DBMS_LOB.APPEND(p_buffer, '"columns":{');
    FOR i IN 1 .. l_cols.COUNT LOOP
      l_low_dec  := decode_raw_value(l_cols(i).low_value,  l_cols(i).data_type);
      l_high_dec := decode_raw_value(l_cols(i).high_value, l_cols(i).data_type);
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        js_string(l_cols(i).col_name) || ':{'
        || '"data_type":' || js_string(l_cols(i).data_type)
        || ',"nullable":' || js_bool(l_cols(i).nullable = 'Y')
        || ',"num_distinct":' || js_number(l_cols(i).num_distinct)
        || ',"num_nulls":' || js_number(l_cols(i).num_nulls)
        || ',"low_value":' || js_string(l_low_dec)
        || ',"high_value":' || js_string(l_high_dec)
        || ',"density":' || js_number(l_cols(i).density)
        || ',"histogram":{'
        || '"type":' || js_string(l_cols(i).histogram)
        || ',"buckets":' || js_number(l_cols(i).hist_buckets)
        || '}'
        || CASE WHEN l_cols(i).virtual_col = 'YES' THEN ',"virtual":true' ELSE '' END
        || CASE WHEN l_cols(i).hidden_col = 'YES' THEN ',"hidden":true' ELSE '' END
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, '}');
  END;

  -- Segment size (bytes/extents summed across all partitions/subpartitions
  -- of the object). Used for both tables and indexes - the segment name for
  -- a non-partitioned object is simply the object name, and for a partitioned
  -- one this SUM naturally aggregates the partition-level segments too.
  -- DBA_SEGMENTS requires a privilege many schemas lack; USER_SEGMENTS is the
  -- fallback, but it only ever shows the connected schema's own segments, so
  -- it is only attempted when p_owner matches the current schema. Otherwise
  -- the field degrades to null with a single one-shot coverage warning.
  PROCEDURE write_segment(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_bytes   NUMBER;
    l_extents NUMBER;
  BEGIN
    IF g_use_dba_seg THEN
      BEGIN
        EXECUTE IMMEDIATE
          'SELECT SUM(bytes), SUM(extents) FROM dba_segments WHERE owner = :1 AND segment_name = :2'
          INTO l_bytes, l_extents USING p_owner, p_name;
      EXCEPTION
        WHEN OTHERS THEN
          IF SQLCODE = -942 THEN
            g_use_dba_seg := FALSE;
          ELSE
            RAISE;
          END IF;
      END;
    END IF;

    IF NOT g_use_dba_seg THEN
      IF p_owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') THEN
        BEGIN
          EXECUTE IMMEDIATE
            'SELECT SUM(bytes), SUM(extents) FROM user_segments WHERE segment_name = :1'
            INTO l_bytes, l_extents USING p_name;
        EXCEPTION
          WHEN OTHERS THEN
            l_bytes := NULL; l_extents := NULL;
        END;
      ELSE
        l_bytes := NULL; l_extents := NULL;
        IF NOT g_seg_warned THEN
          g_seg_warned := TRUE;
          add_warning('DBA_SEGMENTS',
            'No SELECT on DBA_SEGMENTS - segment sizes unavailable for objects not owned '
            || 'by the connected schema.');
        END IF;
      END IF;
    END IF;

    IF l_bytes IS NULL AND l_extents IS NULL THEN
      DBMS_LOB.APPEND(p_buffer, '"segment":null');
    ELSE
      DBMS_LOB.APPEND(p_buffer,
        '"segment":{"bytes":' || js_number(l_bytes) || ',"extents":' || js_number(l_extents) || '}');
    END IF;
  END;

  -- Physical storage attributes for a table. Best-effort: any failure leaves
  -- every field null rather than raising - write_table_stats already emits a
  -- coverage warning when the table itself can't be found.
  PROCEDURE write_table_physical(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_view          VARCHAR2(64);
    l_compression   VARCHAR2(32);
    l_compress_for  VARCHAR2(32);
    l_degree        VARCHAR2(64);
    l_temporary     VARCHAR2(1);
    l_cluster_name  VARCHAR2(128);
    l_iot_type      VARCHAR2(32);
    l_cache         VARCHAR2(64);
  BEGIN
    l_view := CASE WHEN g_use_dba_tables THEN 'dba_tables' ELSE 'all_tables' END;
    BEGIN
      EXECUTE IMMEDIATE
        'SELECT compression, compress_for, TRIM(degree), temporary, cluster_name, '
        || '       iot_type, TRIM(cache) '
        || 'FROM ' || l_view || ' WHERE owner = :1 AND table_name = :2'
        INTO l_compression, l_compress_for, l_degree, l_temporary, l_cluster_name,
             l_iot_type, l_cache
        USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN NULL; -- leave all fields null
    END;

    DBMS_LOB.APPEND(p_buffer,
      '"physical":{'
      || '"compression":' || js_string(l_compression)
      || ',"compress_for":' || js_string(l_compress_for)
      || ',"degree":' || js_string(l_degree)
      || ',"temporary":' || js_bool(l_temporary = 'Y')
      || ',"cluster_name":' || js_string(l_cluster_name)
      || ',"iot_type":' || js_string(l_iot_type)
      || ',"cache":' || js_bool(l_cache = 'Y')
      || '}'
    );
  END;

  -- PK/UK/FK/CHECK constraints for a table. Uses {DBA|ALL}_CONSTRAINTS +
  -- {DBA|ALL}_CONS_COLUMNS (ordered by position). Foreign keys resolve their
  -- referenced table + columns via a second lookup on r_owner/r_constraint_name.
  -- CHECK conditions use SEARCH_CONDITION_VC (12.2+); on ORA-00904 (older DBs
  -- only expose the LONG SEARCH_CONDITION column, which is deliberately never
  -- selected here) the condition degrades to null with a single warning.
  PROCEDURE write_constraints(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_view       VARCHAR2(64);
    l_stmt       VARCHAR2(2000);
    l_first      BOOLEAN;
    l_pk_emitted BOOLEAN := FALSE;

    TYPE t_cons IS RECORD (
      constraint_name   VARCHAR2(128),
      constraint_type   VARCHAR2(1),
      status            VARCHAR2(8),
      validated         VARCHAR2(13),
      generated         VARCHAR2(14),
      r_owner           VARCHAR2(128),
      r_constraint_name VARCHAR2(128),
      delete_rule       VARCHAR2(9)
    );
    TYPE t_cons_tab IS TABLE OF t_cons;
    l_cons t_cons_tab := t_cons_tab();

    TYPE t_check IS RECORD (
      constraint_name VARCHAR2(128),
      status          VARCHAR2(8),
      generated       VARCHAR2(14),
      condition       VARCHAR2(4000)
    );
    TYPE t_check_tab IS TABLE OF t_check;
    l_checks t_check_tab := t_check_tab();

    FUNCTION cons_columns(p_cons_name IN VARCHAR2) RETURN SYS.ODCIVARCHAR2LIST IS
      l_cols_view VARCHAR2(64);
      l_cols      SYS.ODCIVARCHAR2LIST;
    BEGIN
      l_cols_view := CASE WHEN g_use_dba_constr THEN 'dba_cons_columns' ELSE 'all_cons_columns' END;
      EXECUTE IMMEDIATE
        'SELECT column_name FROM ' || l_cols_view
        || ' WHERE owner = :1 AND constraint_name = :2 ORDER BY position'
        BULK COLLECT INTO l_cols USING p_owner, p_cons_name;
      RETURN l_cols;
    EXCEPTION
      WHEN OTHERS THEN RETURN SYS.ODCIVARCHAR2LIST();
    END;

    FUNCTION cols_json(p_cols IN SYS.ODCIVARCHAR2LIST) RETURN VARCHAR2 IS
      l_out VARCHAR2(4000) := '[';
      l_f   BOOLEAN := TRUE;
    BEGIN
      FOR i IN 1 .. p_cols.COUNT LOOP
        IF NOT l_f THEN l_out := l_out || ','; END IF;
        l_f := FALSE;
        l_out := l_out || js_string(p_cols(i));
      END LOOP;
      RETURN l_out || ']';
    END;

    -- Resolve the table + ordered columns a FK references, via its
    -- r_owner/r_constraint_name pointer into the (PK/unique) constraint it targets.
    PROCEDURE resolve_fk_target(
      p_r_owner IN VARCHAR2,
      p_r_cons  IN VARCHAR2,
      p_table   OUT VARCHAR2,
      p_cols    OUT VARCHAR2
    ) IS
      l_cview VARCHAR2(64);
      l_table VARCHAR2(128);
    BEGIN
      l_cview := CASE WHEN g_use_dba_constr THEN 'dba_constraints' ELSE 'all_constraints' END;
      BEGIN
        EXECUTE IMMEDIATE
          'SELECT table_name FROM ' || l_cview || ' WHERE owner = :1 AND constraint_name = :2'
          INTO l_table USING p_r_owner, p_r_cons;
      EXCEPTION
        WHEN OTHERS THEN l_table := NULL;
      END;
      p_table := l_table;
      -- The FK's own owner may differ from the referenced constraint's owner
      -- (cross-schema FKs), so column lookup must use p_r_owner, not p_owner.
      DECLARE
        l_cols_view VARCHAR2(64);
        l_cols      SYS.ODCIVARCHAR2LIST;
      BEGIN
        l_cols_view := CASE WHEN g_use_dba_constr THEN 'dba_cons_columns' ELSE 'all_cons_columns' END;
        EXECUTE IMMEDIATE
          'SELECT column_name FROM ' || l_cols_view
          || ' WHERE owner = :1 AND constraint_name = :2 ORDER BY position'
          BULK COLLECT INTO l_cols USING p_r_owner, p_r_cons;
        p_cols := cols_json(l_cols);
      EXCEPTION
        WHEN OTHERS THEN p_cols := '[]';
      END;
    END;
  BEGIN
    l_view := CASE WHEN g_use_dba_constr THEN 'dba_constraints' ELSE 'all_constraints' END;
    l_stmt := 'SELECT constraint_name, constraint_type, status, validated, generated, '
           || '       r_owner, r_constraint_name, delete_rule '
           || 'FROM ' || l_view || ' '
           || 'WHERE owner = :1 AND table_name = :2 AND constraint_type IN (''P'',''U'',''R'') '
           || 'ORDER BY constraint_name';
    BEGIN
      EXECUTE IMMEDIATE l_stmt BULK COLLECT INTO l_cons USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE = -942 AND g_use_dba_constr THEN
          g_use_dba_constr := FALSE;
          write_constraints(p_owner, p_name, p_buffer);
          RETURN;
        END IF;
        add_warning(p_owner || '.' || p_name, 'Constraint query failed: ' || SQLERRM);
        DBMS_LOB.APPEND(p_buffer,
          '"constraints":{"primary_key":null,"unique":[],"foreign_keys":[],"checks":[]}');
        RETURN;
    END;

    l_stmt := 'SELECT constraint_name, status, generated, search_condition_vc '
           || 'FROM ' || l_view || ' '
           || 'WHERE owner = :1 AND table_name = :2 AND constraint_type = ''C'' '
           || 'ORDER BY constraint_name';
    BEGIN
      EXECUTE IMMEDIATE l_stmt BULK COLLECT INTO l_checks USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE = -904 THEN
          BEGIN
            EXECUTE IMMEDIATE
              'SELECT constraint_name, status, generated, CAST(NULL AS VARCHAR2(1)) '
              || 'FROM ' || l_view || ' '
              || 'WHERE owner = :1 AND table_name = :2 AND constraint_type = ''C'' '
              || 'ORDER BY constraint_name'
              BULK COLLECT INTO l_checks USING p_owner, p_name;
            add_warning(p_owner || '.' || p_name,
              'CHECK constraint text not captured (SEARCH_CONDITION_VC unavailable pre-12.2).');
          EXCEPTION
            WHEN OTHERS THEN l_checks := t_check_tab();
          END;
        ELSIF SQLCODE = -942 THEN
          l_checks := t_check_tab();
        ELSE
          add_warning(p_owner || '.' || p_name, 'CHECK constraint query failed: ' || SQLERRM);
          l_checks := t_check_tab();
        END IF;
    END;

    DBMS_LOB.APPEND(p_buffer, '"constraints":{');

    DBMS_LOB.APPEND(p_buffer, '"primary_key":');
    FOR i IN 1 .. l_cons.COUNT LOOP
      IF l_cons(i).constraint_type = 'P' THEN
        DBMS_LOB.APPEND(p_buffer,
          '{"name":' || js_string(l_cons(i).constraint_name)
          || ',"columns":' || cols_json(cons_columns(l_cons(i).constraint_name))
          || ',"status":' || js_string(l_cons(i).status)
          || ',"validated":' || js_string(l_cons(i).validated)
          || '}'
        );
        l_pk_emitted := TRUE;
        EXIT;
      END IF;
    END LOOP;
    IF NOT l_pk_emitted THEN
      DBMS_LOB.APPEND(p_buffer, 'null');
    END IF;

    DBMS_LOB.APPEND(p_buffer, ',"unique":[');
    l_first := TRUE;
    FOR i IN 1 .. l_cons.COUNT LOOP
      IF l_cons(i).constraint_type = 'U' THEN
        IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
        l_first := FALSE;
        DBMS_LOB.APPEND(p_buffer,
          '{"name":' || js_string(l_cons(i).constraint_name)
          || ',"columns":' || cols_json(cons_columns(l_cons(i).constraint_name))
          || ',"status":' || js_string(l_cons(i).status)
          || ',"validated":' || js_string(l_cons(i).validated)
          || '}'
        );
      END IF;
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, ',"foreign_keys":[');
    l_first := TRUE;
    FOR i IN 1 .. l_cons.COUNT LOOP
      IF l_cons(i).constraint_type = 'R' THEN
        DECLARE
          l_ref_table VARCHAR2(128);
          l_ref_cols  VARCHAR2(4000);
        BEGIN
          resolve_fk_target(l_cons(i).r_owner, l_cons(i).r_constraint_name, l_ref_table, l_ref_cols);
          IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
          l_first := FALSE;
          DBMS_LOB.APPEND(p_buffer,
            '{"name":' || js_string(l_cons(i).constraint_name)
            || ',"columns":' || cols_json(cons_columns(l_cons(i).constraint_name))
            || ',"ref_owner":' || js_string(l_cons(i).r_owner)
            || ',"ref_table":' || js_string(l_ref_table)
            || ',"ref_columns":' || l_ref_cols
            || ',"delete_rule":' || js_string(l_cons(i).delete_rule)
            || ',"status":' || js_string(l_cons(i).status)
            || ',"validated":' || js_string(l_cons(i).validated)
            || '}'
          );
        END;
      END IF;
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, ',"checks":[');
    l_first := TRUE;
    FOR i IN 1 .. l_checks.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"name":' || js_string(l_checks(i).constraint_name)
        || ',"condition":' || js_string(l_checks(i).condition)
        || ',"status":' || js_string(l_checks(i).status)
        || ',"generated":' || js_bool(l_checks(i).generated = 'GENERATED NAME')
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, '}');
  END;

  -- Extended statistics (column groups / expression stats) for a table, e.g.
  -- entries created by DBMS_STATS.CREATE_EXTENDED_STATS. has_histogram checks
  -- whether the synthetic hidden column backing the extension (its name,
  -- e.g. SYS_STUxxxxxxxxxxxxxxxxxxxxxxxxxx, equals EXTENSION_NAME) carries a
  -- histogram - i.e. whether the extension itself has been analyzed.
  PROCEDURE write_extended_stats(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_view      VARCHAR2(64);
    l_cols_view VARCHAR2(64);
    l_stmt      VARCHAR2(4000);
    l_first     BOOLEAN := TRUE;
    TYPE t_ext IS RECORD (
      extension_name VARCHAR2(30),
      extension      VARCHAR2(4000),
      has_histogram  VARCHAR2(3)
    );
    TYPE t_ext_tab IS TABLE OF t_ext;
    l_exts t_ext_tab;
  BEGIN
    l_view := CASE WHEN g_use_dba_ext THEN 'dba_stat_extensions' ELSE 'all_stat_extensions' END;
    l_cols_view := CASE WHEN g_use_dba_cols THEN 'dba_tab_cols' ELSE 'all_tab_cols' END;
    l_stmt :=
      'SELECT e.extension_name, SUBSTR(e.extension, 1, 4000), '
      || '       CASE WHEN EXISTS (SELECT 1 FROM ' || l_cols_view || ' c '
      || '                          WHERE c.owner = e.owner AND c.table_name = e.table_name '
      || '                          AND c.column_name = e.extension_name '
      || '                          AND NVL(c.histogram, ''NONE'') <> ''NONE'') '
      || '            THEN ''YES'' ELSE ''NO'' END '
      || 'FROM ' || l_view || ' e WHERE e.owner = :1 AND e.table_name = :2';
    BEGIN
      EXECUTE IMMEDIATE l_stmt BULK COLLECT INTO l_exts USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE = -942 AND g_use_dba_ext THEN
          g_use_dba_ext := FALSE;
          write_extended_stats(p_owner, p_name, p_buffer);
          RETURN;
        END IF;
        add_warning(p_owner || '.' || p_name, 'Extended stats query failed: ' || SQLERRM);
        DBMS_LOB.APPEND(p_buffer, '"extended_stats":[]');
        RETURN;
    END;

    DBMS_LOB.APPEND(p_buffer, '"extended_stats":[');
    FOR i IN 1 .. l_exts.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"extension_name":' || js_string(l_exts(i).extension_name)
        || ',"extension":' || js_string(l_exts(i).extension)
        || ',"has_histogram":' || js_bool(l_exts(i).has_histogram = 'YES')
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');
  END;

  PROCEDURE write_indexes_list(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_view   VARCHAR2(64);
    l_stmt   VARCHAR2(2000);
    l_first  BOOLEAN := TRUE;
    TYPE t_idx IS TABLE OF VARCHAR2(257);
    l_idx_keys t_idx;
  BEGIN
    l_view := CASE WHEN g_use_dba_indexes THEN 'dba_indexes' ELSE 'all_indexes' END;
    l_stmt := 'SELECT owner || ''.'' || index_name FROM ' || l_view
           || ' WHERE table_owner = :1 AND table_name = :2 ORDER BY index_name';
    BEGIN
      EXECUTE IMMEDIATE l_stmt BULK COLLECT INTO l_idx_keys USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE = -942 AND g_use_dba_indexes THEN
          g_use_dba_indexes := FALSE;
          write_indexes_list(p_owner, p_name, p_buffer);
          RETURN;
        END IF;
        DBMS_LOB.APPEND(p_buffer, '"indexes":[]');
        RETURN;
    END;
    DBMS_LOB.APPEND(p_buffer, '"indexes":[');
    FOR i IN 1 .. l_idx_keys.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer, js_string(l_idx_keys(i)));
      add_object(SUBSTR(l_idx_keys(i), 1, INSTR(l_idx_keys(i), '.') - 1),
                 SUBSTR(l_idx_keys(i), INSTR(l_idx_keys(i), '.') + 1),
                 'INDEX');
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');
  END;

  PROCEDURE write_index_object(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
    l_view        VARCHAR2(64);
    l_uniqueness  VARCHAR2(32);
    l_index_type  VARCHAR2(64);
    l_status      VARCHAR2(32);
    l_visibility  VARCHAR2(32);
    l_partitioned VARCHAR2(3);
    l_cf          NUMBER;
    l_blevel      NUMBER;
    l_leaf        NUMBER;
    l_distinct    NUMBER;
    l_table_owner VARCHAR2(128);
    l_table_name  VARCHAR2(128);
    l_part_type   VARCHAR2(32);
    l_locality    VARCHAR2(32);
    l_num_rows    NUMBER;
    l_avg_leaf    NUMBER;
    l_avg_data    NUMBER;
    l_last_analyzed DATE;
    l_degree      VARCHAR2(64);
    l_compression VARCHAR2(32);
    l_stmt        VARCHAR2(2000);
    l_cols_stmt   VARCHAR2(2000);
    l_cols_view   VARCHAR2(64);
    l_cols        SYS.ODCIVARCHAR2LIST;
    l_first       BOOLEAN := TRUE;
  BEGIN
    l_view := CASE WHEN g_use_dba_indexes THEN 'dba_indexes' ELSE 'all_indexes' END;
    l_stmt := 'SELECT uniqueness, index_type, status, visibility, partitioned, '
           || '       clustering_factor, blevel, leaf_blocks, distinct_keys, '
           || '       table_owner, table_name, num_rows, avg_leaf_blocks_per_key, '
           || '       avg_data_blocks_per_key, last_analyzed, TRIM(degree), compression '
           || 'FROM ' || l_view || ' WHERE owner = :1 AND index_name = :2';
    BEGIN
      EXECUTE IMMEDIATE l_stmt
        INTO l_uniqueness, l_index_type, l_status, l_visibility, l_partitioned,
             l_cf, l_blevel, l_leaf, l_distinct,
             l_table_owner, l_table_name, l_num_rows, l_avg_leaf,
             l_avg_data, l_last_analyzed, l_degree, l_compression
        USING p_owner, p_name;
    EXCEPTION
      WHEN NO_DATA_FOUND THEN
        add_warning(p_owner || '.' || p_name, 'Index not found in ' || l_view);
        RETURN;
      WHEN OTHERS THEN
        IF SQLCODE = -942 AND g_use_dba_indexes THEN
          g_use_dba_indexes := FALSE;
          write_index_object(p_owner, p_name, p_buffer);
          RETURN;
        END IF;
        add_warning(p_owner || '.' || p_name, 'Index stats query failed: ' || SQLERRM);
        RETURN;
    END;

    l_cols_view := CASE WHEN g_use_dba_indexes THEN 'dba_ind_columns' ELSE 'all_ind_columns' END;
    l_cols_stmt := 'SELECT column_name FROM ' || l_cols_view
                || ' WHERE index_owner = :1 AND index_name = :2 ORDER BY column_position';
    BEGIN
      EXECUTE IMMEDIATE l_cols_stmt BULK COLLECT INTO l_cols USING p_owner, p_name;
    EXCEPTION
      WHEN OTHERS THEN l_cols := SYS.ODCIVARCHAR2LIST();
    END;

    -- Partition method + locality (LOCAL/GLOBAL) for partitioned indexes only.
    IF l_partitioned = 'YES' THEN
      BEGIN
        EXECUTE IMMEDIATE
          'SELECT partitioning_type, locality FROM '
          || CASE WHEN g_use_dba_indexes THEN 'dba_part_indexes' ELSE 'all_part_indexes' END
          || ' WHERE owner = :1 AND index_name = :2'
          INTO l_part_type, l_locality USING p_owner, p_name;
      EXCEPTION
        WHEN OTHERS THEN
          BEGIN
            EXECUTE IMMEDIATE
              'SELECT partitioning_type, locality FROM all_part_indexes'
              || ' WHERE owner = :1 AND index_name = :2'
              INTO l_part_type, l_locality USING p_owner, p_name;
          EXCEPTION
            WHEN OTHERS THEN
              l_part_type := NULL; l_locality := NULL;
          END;
      END;
    END IF;

    DBMS_LOB.APPEND(p_buffer,
      '"' || p_owner || '.' || p_name || '":{'
      || '"type":"INDEX"'
      || ',"stats":{'
      || '"uniqueness":' || js_string(l_uniqueness)
      || ',"index_type":' || js_string(l_index_type)
      || ',"status":' || js_string(l_status)
      || ',"visibility":' || js_string(l_visibility)
      || ',"partitioned":' || js_bool(l_partitioned = 'YES')
      || CASE WHEN l_partitioned = 'YES' THEN
             ',"partition_type":' || js_string(l_part_type)
             || ',"locality":' || js_string(l_locality)
             || ',"partition_key":' || js_part_key_cols(p_owner, p_name, 'INDEX', FALSE, g_use_dba_indexes)
           ELSE '' END
      || ',"clustering_factor":' || js_number(l_cf)
      || ',"blevel":' || js_number(l_blevel)
      || ',"leaf_blocks":' || js_number(l_leaf)
      || ',"distinct_keys":' || js_number(l_distinct)
      || ',"num_rows":' || js_number(l_num_rows)
      || ',"avg_leaf_blocks_per_key":' || js_number(l_avg_leaf)
      || ',"avg_data_blocks_per_key":' || js_number(l_avg_data)
      || ',"last_analyzed":' || js_iso_ts(l_last_analyzed)
      || ',"degree":' || js_string(l_degree)
      || ',"compression":' || js_string(l_compression)
      || '}'
      || ',"columns":['
    );
    FOR i IN 1 .. l_cols.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer, js_string(l_cols(i)));
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');
    DBMS_LOB.APPEND(p_buffer,
      ',"table":' || js_string(l_table_owner || '.' || l_table_name)
    );
    DBMS_LOB.APPEND(p_buffer, ',');
    write_segment(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    append_ddl_field(p_buffer, get_object_ddl('INDEX', p_owner, p_name));
    DBMS_LOB.APPEND(p_buffer, '}');
  END;

  PROCEDURE write_table_object(
    p_owner   IN VARCHAR2,
    p_name    IN VARCHAR2,
    p_buffer  IN OUT NOCOPY CLOB
  ) IS
  BEGIN
    DBMS_LOB.APPEND(p_buffer,
      '"' || p_owner || '.' || p_name || '":{'
      || '"type":"TABLE",'
    );
    write_table_stats(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    write_table_physical(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    write_segment(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    write_columns(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    write_constraints(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    write_extended_stats(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    write_indexes_list(p_owner, p_name, p_buffer);
    DBMS_LOB.APPEND(p_buffer, ',');
    append_ddl_field(p_buffer, get_object_ddl('TABLE', p_owner, p_name));
    DBMS_LOB.APPEND(p_buffer, '}');
  END;

  PROCEDURE write_objects(p_buffer IN OUT NOCOPY CLOB) IS
    l_first   BOOLEAN := TRUE;
    l_count   PLS_INTEGER;
    l_type    VARCHAR2(64);
  BEGIN
    DBMS_LOB.APPEND(p_buffer, '"objects":{');
    -- Tables first. V$SQL_PLAN object_type carries decorations like
    -- 'TABLE (TEMP)' or 'INDEX (UNIQUE)', so match by prefix, not equality.
    l_count := l_objects.COUNT;
    FOR i IN 1 .. l_count LOOP
      l_type := l_objects(i).type;
      IF l_type LIKE 'TABLE%' OR l_type LIKE 'MAT_VIEW%' OR l_type = 'UNKNOWN' THEN
        IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
        l_first := FALSE;
        write_table_object(l_objects(i).owner, l_objects(i).name, p_buffer);
      END IF;
    END LOOP;
    -- Indexes (may have grown the collection above)
    l_count := l_objects.COUNT;
    FOR i IN 1 .. l_count LOOP
      l_type := l_objects(i).type;
      IF l_type LIKE 'INDEX%' THEN
        IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
        l_first := FALSE;
        write_index_object(l_objects(i).owner, l_objects(i).name, p_buffer);
      END IF;
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, '}');
  END;

  PROCEDURE write_warnings(p_buffer IN OUT NOCOPY CLOB) IS
    l_first BOOLEAN := TRUE;
  BEGIN
    DBMS_LOB.APPEND(p_buffer, '"coverage_warnings":[');
    FOR i IN 1 .. l_warnings.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"object":' || js_string(l_warnings(i).object)
        || ',"reason":' || js_string(l_warnings(i).reason)
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');
  END;

  PROCEDURE write_system_params(p_buffer IN OUT NOCOPY CLOB) IS
    l_block_size  VARCHAR2(64);
    l_ofe         VARCHAR2(64);
    l_index_cost  VARCHAR2(64);
    l_index_cache VARCHAR2(64);

    -- Dynamic so a non-privileged user (no SELECT on V$PARAMETER) degrades to
    -- null values at runtime instead of an ORA-06550 compile failure. The
    -- function body must follow the item declarations above (PL/SQL rule).
    FUNCTION get_param(p_name IN VARCHAR2) RETURN VARCHAR2 IS
      l_val VARCHAR2(512);
    BEGIN
      EXECUTE IMMEDIATE 'SELECT value FROM v$parameter WHERE name = :1'
        INTO l_val USING p_name;
      RETURN l_val;
    EXCEPTION WHEN OTHERS THEN RETURN NULL;
    END;
  BEGIN
    l_block_size  := get_param('db_block_size');
    l_ofe         := get_param('optimizer_features_enable');
    l_index_cost  := get_param('optimizer_index_cost_adj');
    l_index_cache := get_param('optimizer_index_caching');

    DBMS_LOB.APPEND(p_buffer,
      '"system_params":{'
      || '"db_block_size":' || CASE WHEN l_block_size IS NULL THEN 'null' ELSE l_block_size END
      || ',"optimizer_features_enable":' || js_string(l_ofe)
      || ',"optimizer_index_cost_adj":' || CASE WHEN l_index_cost IS NULL THEN 'null' ELSE l_index_cost END
      || ',"optimizer_index_caching":' || CASE WHEN l_index_cache IS NULL THEN 'null' ELSE l_index_cache END
      || '}'
    );
  END;

  -- Non-default optimizer environment parameters for the cursor's SQL_ID
  -- (SQL_ID mode only - there is no cursor to inspect in LIST mode). Picks a
  -- single child cursor from V$SQL, then reads the params V$SQL_OPTIMIZER_ENV
  -- flags as ISDEFAULT = 'NO' for that child. Any failure (no privilege, cursor
  -- aged out of the shared pool, ...) degrades to an empty array + warning.
  PROCEDURE write_optimizer_env(p_buffer IN OUT NOCOPY CLOB) IS
    l_child NUMBER;
    l_first BOOLEAN := TRUE;
    TYPE t_env IS RECORD (name VARCHAR2(80), value VARCHAR2(4000));
    TYPE t_env_tab IS TABLE OF t_env;
    l_envs t_env_tab;
  BEGIN
    BEGIN
      EXECUTE IMMEDIATE
        'SELECT MIN(child_number) FROM v$sql WHERE sql_id = :1 '
        || 'AND (:2 IS NULL OR plan_hash_value = :3)'
        INTO l_child USING g_sql_id, g_plan_hash, g_plan_hash;
    EXCEPTION
      WHEN OTHERS THEN l_child := NULL;
    END;

    IF l_child IS NULL THEN
      add_warning(NVL(g_sql_id, '(no sql_id)'),
        'Could not resolve a child cursor in V$SQL for optimizer_env.');
      DBMS_LOB.APPEND(p_buffer, '"optimizer_env":[]');
      RETURN;
    END IF;

    BEGIN
      EXECUTE IMMEDIATE
        'SELECT name, value FROM v$sql_optimizer_env '
        || 'WHERE sql_id = :1 AND child_number = :2 AND isdefault = ''NO'' '
        || 'ORDER BY name'
        BULK COLLECT INTO l_envs USING g_sql_id, l_child;
    EXCEPTION
      WHEN OTHERS THEN
        add_warning(NVL(g_sql_id, '(no sql_id)'), 'V$SQL_OPTIMIZER_ENV query failed: ' || SQLERRM);
        DBMS_LOB.APPEND(p_buffer, '"optimizer_env":[]');
        RETURN;
    END;

    DBMS_LOB.APPEND(p_buffer, '"optimizer_env":[');
    FOR i IN 1 .. l_envs.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"name":' || js_string(l_envs(i).name)
        || ',"value":' || js_string(l_envs(i).value)
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');
  END;

  -- SQL plan management objects associated with this SQL_ID (SQL_ID mode
  -- only): baselines, profiles, patches and plan directives. Each sub-block
  -- is independent and best-effort - a failure in one (e.g. missing
  -- SELECT_CATALOG_ROLE) does not prevent the others from being emitted.
  PROCEDURE write_sql_management(p_buffer IN OUT NOCOPY CLOB) IS
    l_exact_sig   NUMBER;
    l_force_sig   NUMBER;
    l_sql_profile VARCHAR2(64);
    l_first       BOOLEAN;

    TYPE t_baseline IS RECORD (
      plan_name     VARCHAR2(30),
      sql_handle    VARCHAR2(30),
      enabled       VARCHAR2(3),
      accepted      VARCHAR2(3),
      fixed         VARCHAR2(3),
      origin        VARCHAR2(20),
      created       DATE,
      last_modified DATE
    );
    TYPE t_baseline_tab IS TABLE OF t_baseline;
    l_baselines t_baseline_tab := t_baseline_tab();

    TYPE t_profile IS RECORD (
      name           VARCHAR2(30),
      category       VARCHAR2(30),
      status         VARCHAR2(10),
      force_matching VARCHAR2(3),
      created        DATE
    );
    TYPE t_profile_tab IS TABLE OF t_profile;
    l_profiles t_profile_tab := t_profile_tab();

    TYPE t_patch IS RECORD (
      name    VARCHAR2(30),
      status  VARCHAR2(10),
      created DATE
    );
    TYPE t_patch_tab IS TABLE OF t_patch;
    l_patches t_patch_tab := t_patch_tab();

    TYPE t_directive IS RECORD (
      directive_id NUMBER,
      dir_type     VARCHAR2(30),
      state        VARCHAR2(30),
      reason       VARCHAR2(4000),
      last_used    DATE
    );
    TYPE t_directive_tab IS TABLE OF t_directive;
    l_directives t_directive_tab := t_directive_tab();

    TYPE t_dir_obj IS RECORD (
      owner          VARCHAR2(128),
      object_name    VARCHAR2(128),
      subobject_name VARCHAR2(128),
      object_type    VARCHAR2(30)
    );
    TYPE t_dir_obj_tab IS TABLE OF t_dir_obj;

    -- Directive ids already emitted, deduped across the multiple resolved
    -- tables that can share one directive (e.g. a join-cardinality directive).
    TYPE t_seen_tab IS TABLE OF BOOLEAN INDEX BY VARCHAR2(40);
    l_seen t_seen_tab;
  BEGIN
    BEGIN
      EXECUTE IMMEDIATE
        'SELECT MIN(exact_matching_signature), MIN(force_matching_signature), MIN(sql_profile) '
        || 'FROM v$sql WHERE sql_id = :1 AND (:2 IS NULL OR plan_hash_value = :3)'
        INTO l_exact_sig, l_force_sig, l_sql_profile USING g_sql_id, g_plan_hash, g_plan_hash;
    EXCEPTION
      WHEN OTHERS THEN l_exact_sig := NULL; l_force_sig := NULL; l_sql_profile := NULL;
    END;

    -- Baselines
    BEGIN
      IF l_exact_sig IS NOT NULL THEN
        EXECUTE IMMEDIATE
          'SELECT plan_name, sql_handle, enabled, accepted, fixed, origin, created, last_modified '
          || 'FROM dba_sql_plan_baselines WHERE signature = :1'
          BULK COLLECT INTO l_baselines USING l_exact_sig;
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        add_warning('DBA_SQL_PLAN_BASELINES', 'SQL plan baseline lookup failed: ' || SQLERRM);
        l_baselines := t_baseline_tab();
    END;

    -- Profiles
    BEGIN
      IF l_exact_sig IS NOT NULL OR l_force_sig IS NOT NULL OR l_sql_profile IS NOT NULL THEN
        EXECUTE IMMEDIATE
          'SELECT name, category, status, force_matching, created FROM dba_sql_profiles '
          || 'WHERE signature IN (:1, :2) OR name = :3'
          BULK COLLECT INTO l_profiles
          USING NVL(l_exact_sig, -1), NVL(l_force_sig, -1), NVL(l_sql_profile, '~none~');
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        add_warning('DBA_SQL_PROFILES', 'SQL profile lookup failed: ' || SQLERRM);
        l_profiles := t_profile_tab();
    END;

    -- Patches (STATUS column absent on some pre-19c point releases)
    BEGIN
      EXECUTE IMMEDIATE
        'SELECT name, status, created FROM dba_sql_patches WHERE sql_id = :1'
        BULK COLLECT INTO l_patches USING g_sql_id;
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLCODE = -904 THEN
          BEGIN
            EXECUTE IMMEDIATE
              'SELECT name, CAST(NULL AS VARCHAR2(10)), created FROM dba_sql_patches WHERE sql_id = :1'
              BULK COLLECT INTO l_patches USING g_sql_id;
          EXCEPTION
            WHEN OTHERS THEN
              add_warning('DBA_SQL_PATCHES', 'SQL patch lookup failed: ' || SQLERRM);
              l_patches := t_patch_tab();
          END;
        ELSE
          add_warning('DBA_SQL_PATCHES', 'SQL patch lookup failed: ' || SQLERRM);
          l_patches := t_patch_tab();
        END IF;
    END;

    -- Directives, resolved per referenced TABLE object (plan directives are
    -- keyed off the objects they observed cardinality misestimates on).
    BEGIN
      FOR i IN 1 .. l_objects.COUNT LOOP
        IF l_objects(i).type LIKE 'TABLE%' OR l_objects(i).type = 'UNKNOWN' THEN
          DECLARE
            l_dir_ids SYS.ODCINUMBERLIST;
          BEGIN
            BEGIN
              EXECUTE IMMEDIATE
                'SELECT DISTINCT o.directive_id FROM dba_sql_plan_dir_objects o '
                || 'WHERE o.owner = :1 AND o.object_name = :2 AND o.object_type = ''TABLE'''
                BULK COLLECT INTO l_dir_ids USING l_objects(i).owner, l_objects(i).name;
            EXCEPTION
              WHEN OTHERS THEN l_dir_ids := SYS.ODCINUMBERLIST();
            END;

            FOR d IN 1 .. l_dir_ids.COUNT LOOP
              IF NOT l_seen.EXISTS(TO_CHAR(l_dir_ids(d))) THEN
                l_seen(TO_CHAR(l_dir_ids(d))) := TRUE;
                DECLARE
                  l_dir t_directive;
                BEGIN
                  EXECUTE IMMEDIATE
                    'SELECT directive_id, type, state, reason, last_used '
                    || 'FROM dba_sql_plan_directives WHERE directive_id = :1'
                    INTO l_dir.directive_id, l_dir.dir_type, l_dir.state, l_dir.reason, l_dir.last_used
                    USING l_dir_ids(d);
                  l_directives.EXTEND;
                  l_directives(l_directives.COUNT) := l_dir;
                EXCEPTION
                  WHEN OTHERS THEN NULL;
                END;
              END IF;
            END LOOP;
          END;
        END IF;
      END LOOP;
    EXCEPTION
      WHEN OTHERS THEN
        add_warning('DBA_SQL_PLAN_DIRECTIVES', 'SQL plan directive lookup failed: ' || SQLERRM);
    END;

    DBMS_LOB.APPEND(p_buffer, '"sql_management":{');

    DBMS_LOB.APPEND(p_buffer, '"baselines":[');
    l_first := TRUE;
    FOR i IN 1 .. l_baselines.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"plan_name":' || js_string(l_baselines(i).plan_name)
        || ',"sql_handle":' || js_string(l_baselines(i).sql_handle)
        || ',"enabled":' || js_bool(l_baselines(i).enabled = 'YES')
        || ',"accepted":' || js_bool(l_baselines(i).accepted = 'YES')
        || ',"fixed":' || js_bool(l_baselines(i).fixed = 'YES')
        || ',"origin":' || js_string(l_baselines(i).origin)
        || ',"created":' || js_iso_ts(l_baselines(i).created)
        || ',"last_modified":' || js_iso_ts(l_baselines(i).last_modified)
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, ',"profiles":[');
    l_first := TRUE;
    FOR i IN 1 .. l_profiles.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"name":' || js_string(l_profiles(i).name)
        || ',"category":' || js_string(l_profiles(i).category)
        || ',"status":' || js_string(l_profiles(i).status)
        || ',"force_matching":' || js_bool(l_profiles(i).force_matching = 'YES')
        || ',"created":' || js_iso_ts(l_profiles(i).created)
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, ',"patches":[');
    l_first := TRUE;
    FOR i IN 1 .. l_patches.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(p_buffer,
        '{"name":' || js_string(l_patches(i).name)
        || ',"status":' || js_string(l_patches(i).status)
        || ',"created":' || js_iso_ts(l_patches(i).created)
        || '}'
      );
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, ',"directives":[');
    l_first := TRUE;
    FOR i IN 1 .. l_directives.COUNT LOOP
      IF NOT l_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
      l_first := FALSE;
      DECLARE
        l_dir_objs  t_dir_obj_tab;
        l_obj_first BOOLEAN := TRUE;
      BEGIN
        BEGIN
          EXECUTE IMMEDIATE
            'SELECT owner, object_name, subobject_name, object_type '
            || 'FROM dba_sql_plan_dir_objects WHERE directive_id = :1'
            BULK COLLECT INTO l_dir_objs USING l_directives(i).directive_id;
        EXCEPTION
          WHEN OTHERS THEN l_dir_objs := t_dir_obj_tab();
        END;
        DBMS_LOB.APPEND(p_buffer,
          '{"directive_id":' || js_string(TO_CHAR(l_directives(i).directive_id))
          || ',"type":' || js_string(l_directives(i).dir_type)
          || ',"state":' || js_string(l_directives(i).state)
          || ',"reason":' || js_string(l_directives(i).reason)
          || ',"last_used":' || js_iso_ts(l_directives(i).last_used)
          || ',"objects":['
        );
        FOR j IN 1 .. l_dir_objs.COUNT LOOP
          IF NOT l_obj_first THEN DBMS_LOB.APPEND(p_buffer, ','); END IF;
          l_obj_first := FALSE;
          DBMS_LOB.APPEND(p_buffer,
            '{"owner":' || js_string(l_dir_objs(j).owner)
            || ',"object_name":' || js_string(l_dir_objs(j).object_name)
            || ',"subobject_name":' || js_string(l_dir_objs(j).subobject_name)
            || ',"object_type":' || js_string(l_dir_objs(j).object_type)
            || '}'
          );
        END LOOP;
        DBMS_LOB.APPEND(p_buffer, ']}');
      END;
    END LOOP;
    DBMS_LOB.APPEND(p_buffer, ']');

    DBMS_LOB.APPEND(p_buffer, '}');
  END;

BEGIN
  ----------------------------------------------------------------------------
  -- Parse arguments
  ----------------------------------------------------------------------------
  IF '&arg1' = 'LIST' OR '&arg1' = 'list' THEN
    g_mode := 'LIST';
    g_manual_list := '&arg2';
  ELSE
    g_mode := 'SQL_ID';
    g_sql_id := '&arg1';
    BEGIN
      g_plan_hash := TO_NUMBER('&arg2');
    EXCEPTION WHEN OTHERS THEN g_plan_hash := NULL; END;
  END IF;

  ----------------------------------------------------------------------------
  -- Source metadata
  ----------------------------------------------------------------------------
  SELECT SYS_CONTEXT('USERENV', 'CON_NAME') INTO g_container FROM DUAL;
  -- Dynamic SQL for the dictionary/dynamic-performance views below: a plain
  -- schema owner may lack SELECT on V$DATABASE and PRODUCT_COMPONENT_VERSION,
  -- and a static reference would fail the whole block at COMPILE time.
  BEGIN
    EXECUTE IMMEDIATE 'SELECT name FROM v$database' INTO g_db_name;
  EXCEPTION WHEN OTHERS THEN g_db_name := NULL; END;
  BEGIN
    EXECUTE IMMEDIATE 'SELECT version_full FROM product_component_version WHERE ROWNUM = 1'
      INTO g_oracle_version;
  EXCEPTION
    WHEN OTHERS THEN
      BEGIN
        EXECUTE IMMEDIATE 'SELECT version FROM product_component_version WHERE ROWNUM = 1'
          INTO g_oracle_version;
      EXCEPTION WHEN OTHERS THEN g_oracle_version := NULL; END;
  END;

  -- Simplify DBMS_METADATA.GET_DDL output: strip storage/segment/tablespace
  -- physical noise and pretty-print, so the captured DDL shows structure, not
  -- placement. Dynamic (and swallowing errors) so it never breaks the gather -
  -- defaults are fine if the session can't set params or can't reach the pkg.
  BEGIN
    EXECUTE IMMEDIATE q'{BEGIN
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SEGMENT_ATTRIBUTES', FALSE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'TABLESPACE', FALSE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'PRETTY', TRUE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', TRUE);
    END;}';
  EXCEPTION WHEN OTHERS THEN NULL;
  END;

  DBMS_LOB.CREATETEMPORARY(l_buffer, TRUE);

  DBMS_LOB.APPEND(l_buffer,
    '{'
    || '"format":"ora-plan-metadata"'
    || ',"version":2'
    || ',"captured_at":' || js_iso_ts(CAST(SYSTIMESTAMP AS TIMESTAMP))
    || ',"source":{'
    || '"db_name":' || js_string(g_db_name)
    || ',"oracle_version":' || js_string(g_oracle_version)
    || ',"container_name":' || js_string(g_container)
    || '}'
    || ',"plan_ref":{'
    || '"sql_id":' || CASE WHEN g_mode = 'SQL_ID' THEN js_string(g_sql_id) ELSE 'null' END
    || ',"plan_hash_value":' || js_number(g_plan_hash)
    || '}'
    || ','
  );

  ----------------------------------------------------------------------------
  -- Container check - refuse to gather from CDB$ROOT
  ----------------------------------------------------------------------------
  IF g_container = 'CDB$ROOT' THEN
    add_warning('CDB$ROOT',
      'Connected to CDB$ROOT - object statistics live inside PDBs. '
      || 'Reconnect to the appropriate PDB and re-run this script.');
    write_objects(l_buffer);
    DBMS_LOB.APPEND(l_buffer, ',');
    write_warnings(l_buffer);
    DBMS_LOB.APPEND(l_buffer, ',');
    write_system_params(l_buffer);
    DBMS_LOB.APPEND(l_buffer, '}');
  ELSE
    ----------------------------------------------------------------------------
    -- Resolve object set
    ----------------------------------------------------------------------------
    g_use_dba_tables  := probe_dba_view('dba_tab_statistics');
    g_use_dba_cols    := probe_dba_view('dba_tab_cols');
    g_use_dba_indexes := probe_dba_view('dba_indexes');
    g_use_dba_constr  := probe_dba_view('dba_constraints');
    g_use_dba_part    := probe_dba_view('dba_tab_partitions');
    g_use_dba_seg     := probe_dba_view('dba_segments');
    g_use_dba_ext     := probe_dba_view('dba_stat_extensions');

    IF g_mode = 'SQL_ID' THEN
      resolve_objects_for_sql_id;
      IF l_objects.COUNT = 0 THEN
        add_warning(NVL(g_sql_id, '(no sql_id)'),
          'No objects resolved for this SQL_ID - not found in V$SQL_PLAN or DBA_HIST_SQL_PLAN. '
          || 'The cursor may have aged out of the shared pool; re-run the statement first, '
          || 'or use LIST mode with explicit OWNER.OBJECT names.');
      END IF;
    ELSE
      resolve_objects_from_list;
      IF l_objects.COUNT = 0 THEN
        add_warning('LIST', 'Manual object list is empty - nothing gathered.');
      END IF;
    END IF;

    write_objects(l_buffer);
    DBMS_LOB.APPEND(l_buffer, ',');
    write_warnings(l_buffer);
    DBMS_LOB.APPEND(l_buffer, ',');
    write_system_params(l_buffer);
    IF g_mode = 'SQL_ID' THEN
      DBMS_LOB.APPEND(l_buffer, ',');
      write_optimizer_env(l_buffer);
      DBMS_LOB.APPEND(l_buffer, ',');
      write_sql_management(l_buffer);
    END IF;
    DBMS_LOB.APPEND(l_buffer, '}');
  END IF;

  ----------------------------------------------------------------------------
  -- Emit
  ----------------------------------------------------------------------------
  -- DBMS_OUTPUT is line-buffered; emit the bundle in chunks. Each PUT_LINE
  -- inserts a line break at an arbitrary position (possibly mid-string) -
  -- the visualizer strips raw newlines on import, and all legitimate
  -- newlines in values are escaped as \n by js_string. Chunks stay well
  -- under PUT_LINE's 32767-BYTE limit even for multibyte characters.
  DECLARE
    l_amount   INTEGER;
    l_offset   INTEGER := 1;
    l_total    INTEGER := DBMS_LOB.GETLENGTH(l_buffer);
    l_chunk    VARCHAR2(32767);
  BEGIN
    WHILE l_offset <= l_total LOOP
      l_amount := LEAST(8000, l_total - l_offset + 1);
      l_chunk := DBMS_LOB.SUBSTR(l_buffer, l_amount, l_offset);
      DBMS_OUTPUT.PUT_LINE(l_chunk);
      l_offset := l_offset + l_amount;
    END LOOP;
  END;

  DBMS_LOB.FREETEMPORARY(l_buffer);
END;
/

-- @@GEN:CLOSE:BEGIN@@
SPOOL OFF
SET TERMOUT ON
PROMPT Done. Wrote &spool_target - paste its contents into the visualizer's
PROMPT input box (or drop the file onto the input panel) to attach it.
-- @@GEN:CLOSE:END@@

-- Restore SQL*Plus factory defaults for the settings this script changed.
SET HEADING ON
SET FEEDBACK 6
SET PAGESIZE 14
SET LINESIZE 80
SET VERIFY ON
-- @@GEN:CLEANUP:BEGIN@@
UNDEFINE arg1
UNDEFINE arg2
UNDEFINE arg3
UNDEFINE spool_target
UNDEFINE 1
UNDEFINE 2
UNDEFINE 3
-- @@GEN:CLEANUP:END@@
