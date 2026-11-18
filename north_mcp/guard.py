"""Run agent-written SQL for a named user, inside that user's database scope.

No MCP or North code here, so it can be tested against PostgreSQL on its own.

    identity (email)  ->  access map  ->  database role  ->  one read-only statement

Rules:
  * A user who is not in the access map gets nothing (default deny).
  * The query runs on a connection opened as the user's role. Row-level security and
    grants in roles.sql decide what it can see; the SQL text is never inspected.
  * One statement per call, in a read-only transaction that is rolled back, with a
    statement timeout and a row cap.
  * Every call is written to the audit log, including denied ones.
"""

import hashlib
import json
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
ACCESS_FILE = Path(os.environ.get("NORTH_MCP_ACCESS", HERE / "access.example.json"))
AUDIT_FILE = Path(os.environ.get("NORTH_MCP_AUDIT", HERE / "audit.jsonl"))
PG_HOST = os.environ.get("NORTH_MCP_PG_HOST", "localhost")
PG_PORT = os.environ.get("NORTH_MCP_PG_PORT", "5432")
PG_DB = os.environ.get("NORTH_MCP_PG_DB", "txn_dq")
ROLE_PASSWORD = os.environ.get("NORTH_MCP_ROLE_PASSWORD", "localdev")  # demo default
TIMEOUT_MS = 5000
MAX_ROWS = 200

SCHEMA_DOC = """PostgreSQL 16. Tables you may read:

currencies(code CHAR(3) PRIMARY KEY, minor_units SMALLINT)
transactions(txn_id BIGINT PRIMARY KEY, external_ref VARCHAR(40), account_id VARCHAR(20),
             currency CHAR(3), amount NUMERIC(18,4), posted_at TIMESTAMP  -- UTC,
             value_date DATE  -- business date in America/Toronto,
             reversal_of BIGINT, loaded_at TIMESTAMP  -- UTC)
report_runs(report_date DATE PRIMARY KEY, generated_at TIMESTAMP  -- UTC)

One read-only SELECT per call. At most {max_rows} rows are returned."""


def _access_map():
    return json.loads(ACCESS_FILE.read_text())["users"]


def role_for(email):
    """Return the database role for this user, or None."""
    if not email or not isinstance(email, str):
        return None
    return _access_map().get(email.strip().lower())


def _audit(entry):
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **entry}
    with AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return value


def _execute(role, sql):
    """Return (columns, rows, truncated). Raises psycopg.Error."""
    conninfo = psycopg.conninfo.make_conninfo(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=role, password=ROLE_PASSWORD
    )
    with psycopg.connect(conninfo) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
                # prepare=True sends the query with the extended protocol, which refuses
                # more than one statement. Without it "COMMIT; ..." would end the
                # read-only transaction and drop the timeout.
                cur.execute(sql, prepare=True)
                if cur.description is None:
                    raise ValueError("statement returned no result set")
                columns = [col.name for col in cur.description]
                rows = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(rows) > MAX_ROWS
                return columns, rows[:MAX_ROWS], truncated
        finally:
            conn.rollback()


def run_query(email, sql):
    """Run one SELECT as the given user. Always returns a dict and always audits."""
    started = time.monotonic()
    sql = (sql or "").strip().rstrip(";").strip()
    role = role_for(email)
    entry = {
        "tool": "run_query",
        "user": email,
        "role": role,
        "sql": sql,
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest()[:16],
    }

    def finish(result):
        entry.update(
            status=result["status"],
            rows=result.get("row_count", 0),
            error=result.get("error"),
            ms=round((time.monotonic() - started) * 1000),
        )
        _audit(entry)
        return result

    if role is None:
        return finish({"status": "denied", "error": "no data access is configured for this user"})
    if not sql:
        return finish({"status": "error", "error": "empty query"})
    try:
        columns, rows, truncated = _execute(role, sql)
    except psycopg.Error as exc:
        return finish({"status": "error", "error": str(exc).strip().splitlines()[0]})
    except ValueError as exc:
        return finish({"status": "error", "error": str(exc)})
    return finish({
        "status": "ok",
        "columns": columns,
        "rows": [[_json_value(v) for v in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    })


def describe_access(email):
    """Tell the agent what this user can query, so it does not have to guess."""
    role = role_for(email)
    _audit({"tool": "describe_access", "user": email, "role": role,
            "status": "ok" if role else "denied"})
    if role is None:
        return {"status": "denied", "error": "no data access is configured for this user"}
    return {"status": "ok", "role": role, "schema": SCHEMA_DOC.format(max_rows=MAX_ROWS),
            "note": "Rows outside your entitlement are filtered out by the database. "
                    "A total you compute covers only the rows you are allowed to see."}
