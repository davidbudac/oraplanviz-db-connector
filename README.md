# oraplanviz-agent

A minimal local companion agent for the [Oracle Execution Plan
Visualizer](https://davidbudac.github.io). It runs on **your own machine**,
connects to **your** Oracle database using
[python-oracledb](https://python-oracledb.readthedocs.io/) in **thin mode**
(pure Python — no Oracle Instant Client required), and exposes a small
localhost JSON HTTP API that the web app's "Connect to database" panel talks
to directly from your browser.

Your credentials and your execution plan data never leave your machine: the
agent only ever talks to your Oracle database and to your browser on
`127.0.0.1`. The hosted app (including the GitHub Pages-hosted version) never
sees them.

## Install

```bash
# recommended: isolated tool install
pipx install oraplanviz-agent

# or, from a checkout of this repo
cd agent
pip install -e .
```

Only runtime dependency: `oracledb>=2.0` (thin mode, no native client
libraries needed).

## Usage

```bash
oraplanviz-agent
```

This starts the agent on `http://127.0.0.1:8521`, prints a random bearer
token, and prints the allowed CORS origins. Paste the URL and token into the
app's Connect panel.

Useful flags:

```
--port PORT             Port to listen on (default: 8521)
--host HOST             Host to bind to (default: 127.0.0.1 -- do not change
                         this unless you understand the risk)
--allow-origin ORIGIN   Allowed CORS origin (repeatable). Defaults to
                         http://localhost:5173 and http://127.0.0.1:5173.
                         Add https://davidbudac.github.io to use the hosted
                         app against your local agent.
--token TOKEN           Bearer token clients must supply. A random one is
                         generated if omitted.
--dsn DSN               Oracle DSN to connect to on startup, e.g.
                         host:1521/service_name
--user USER             Oracle username to connect with on startup (you will
                         be prompted for the password; it is never accepted
                         as a command-line argument).
```

Example, connecting on startup and allowing the hosted app's origin:

```bash
oraplanviz-agent \
  --allow-origin https://davidbudac.github.io \
  --allow-origin http://localhost:5173 \
  --dsn dbhost.example.com:1521/pdb1.example.com \
  --user planviz
```

You can also connect after startup from the app's Connect panel — it POSTs
`dsn`/`user`/`password` to `/api/connect`.

## Security model

- **Binds to `127.0.0.1` only** (never `0.0.0.0`) — the agent is not reachable
  from other machines on your network.
- **Bearer token**, randomly generated at startup (or set with `--token`),
  required on every `/api/*` request except `/api/health`. This blocks
  drive-by web pages from hitting your agent even though it listens on
  localhost.
- **CORS allowlist** (`--allow-origin`, repeatable) — only requests whose
  `Origin` header matches an allowed origin get `Access-Control-Allow-Origin`
  back, so browsers block cross-origin reads from other sites.
- **Chrome Private Network Access**: the agent answers `OPTIONS` preflights
  carrying `Access-Control-Request-Private-Network: true` with
  `Access-Control-Allow-Private-Network: true`, which Chrome requires for a
  public HTTPS page (like GitHub Pages) to reach `127.0.0.1`.
- **Mixed content**: Chrome and Firefox treat `http://127.0.0.1` as
  "potentially trustworthy", so an `https://` page can fetch it. **Safari
  blocks this** (no localhost exception for mixed content) — Safari users
  should run the app's local dev server (`npm run dev`, `http://localhost`)
  instead of the hosted HTTPS app.
- **Credentials are held in agent process memory only** — never written to
  disk, never logged. They are lost when the agent process exits or you call
  `/api/disconnect`.

## Licensing note

Not every plan source is free to use:

- `source=cursor` (`DBMS_XPLAN.DISPLAY_CURSOR`) — **free**, part of the base
  database.
- `source=monitor` (`DBMS_SQL_MONITOR.REPORT_SQL_MONITOR`) — requires the
  **Oracle Tuning Pack**.
- `source=awr` (`DBMS_XPLAN.DISPLAY_AWR`) — requires the **Oracle Diagnostics
  Pack**.

The app labels the pack-licensed sources in the UI. Default to `cursor`
unless you know you're licensed for the others.

## Minimal DB grants

The agent's DB user needs read access to a handful of dynamic performance
views, plus implicit execute on the `DBMS_XPLAN`/`DBMS_SQL_MONITOR` packages
(these are typically already grantable via `SELECT_CATALOG_ROLE` or your DBA
team's standard read-only role):

```sql
GRANT SELECT ON v$sql TO planviz_agent_user;
GRANT SELECT ON v$sql_monitor TO planviz_agent_user;
GRANT SELECT ON v$sql_plan TO planviz_agent_user;
GRANT SELECT ON v$sql_plan_statistics_all TO planviz_agent_user;
-- DBMS_XPLAN.DISPLAY_CURSOR / DISPLAY_AWR and DBMS_SQL_MONITOR.REPORT_SQL_MONITOR
-- are typically EXECUTE-able by any authenticated user; if not:
GRANT EXECUTE ON DBMS_XPLAN TO planviz_agent_user;
GRANT EXECUTE ON DBMS_SQL_MONITOR TO planviz_agent_user;
```

`source=awr` additionally requires access to `DBA_HIST_*` views (Diagnostics
Pack).

## API reference

All responses are JSON. All `/api/*` endpoints except `/api/health` require
an `Authorization: Bearer <token>` header.

| Method | Path                | Description |
|--------|---------------------|--------------|
| GET    | `/api/health`       | `{ version, connected, oracleVersion }` — no auth required. |
| POST   | `/api/connect`      | Body `{ dsn, user, password }` → `{ ok, oracleVersion }`. |
| POST   | `/api/disconnect`   | → `{ ok: true }`. |
| GET    | `/api/sql/recent`   | Query `?source=cursor|monitor` → `{ items: [...] }`. |
| GET    | `/api/plan`         | Query `?sqlId=&source=cursor|monitor|awr&childNumber=&sqlExecId=` → `{ source, text }` (raw plan text — the app auto-detects the format). |

## Development

```bash
cd agent
pip install -e ".[dev]"
python3 -m pytest -q
```

Tests run without the real `oracledb` driver installed — `db.py` imports it
lazily and the test suite monkeypatches a fake driver in its place.
