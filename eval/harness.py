"""Execution-based grader for SQL written by a model.

    python eval/harness.py selftest
    python eval/harness.py run --model <model-id>

A candidate query is run as the read-only role `agent_ro` inside a transaction that
is always rolled back, with a statement timeout. Its result set is compared with the
reference query's result set as a multiset of rows: row order and column names are
ignored, column order and duplicates are not.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

from tasks import SCHEMA_DOC, TASKS

DSN = os.environ.get("AGENT_DSN", "postgresql://agent_ro:localdev@localhost:5432/txn_dq")
TIMEOUT_MS = 5000
MAX_ROWS = 10000
MAX_TRIES = 6                                   # per model call, with backoff
RETRY_STATUS = (408, 429, 500, 502, 503, 504)

SYSTEM_PROMPT = (
    "You write PostgreSQL 16 queries. Reply with exactly one SQL query in a ```sql fenced "
    "block and nothing else. The query must be a single read-only SELECT statement that "
    "returns exactly the requested columns in the requested order."
)


def run_sql(sql):
    """Return (rows, error). Exactly one of the two is None."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return None, "empty query"
    try:
        with psycopg.connect(DSN) as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
                    cur.execute(sql)
                    if cur.description is None:
                        return None, "statement returned no result set"
                    rows = cur.fetchmany(MAX_ROWS + 1)
                    if len(rows) > MAX_ROWS:
                        return None, f"more than {MAX_ROWS} rows"
                    return rows, None
            finally:
                conn.rollback()
    except psycopg.Error as exc:
        return None, str(exc).strip().splitlines()[0]


def _norm(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        return str(Decimal(str(value)).quantize(Decimal("0.0001")))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value).rstrip()


def compare(expected, actual):
    """Return (passed, reason)."""
    if expected and actual and len(expected[0]) != len(actual[0]):
        return False, f"expected {len(expected[0])} columns, got {len(actual[0])}"
    if len(expected) != len(actual):
        return False, f"expected {len(expected)} rows, got {len(actual)}"
    want = Counter(tuple(_norm(v) for v in row) for row in expected)
    got = Counter(tuple(_norm(v) for v in row) for row in actual)
    if want != got:
        return False, f"{sum((want - got).values())} of {len(expected)} rows differ"
    return True, "match"


def grade(task, candidate_sql):
    expected, err = run_sql(task["reference"])
    if err:
        raise RuntimeError(f"reference query for {task['id']} failed: {err}")
    actual, err = run_sql(candidate_sql)
    if err:
        return False, f"error: {err}"
    return compare(expected, actual)


def extract_sql(text):
    blocks = re.findall(r"```(?:sql|postgresql|postgres)?\s*\n(.*?)```", text or "", re.DOTALL | re.IGNORECASE)
    return (blocks[-1] if blocks else (text or "")).strip()


class ModelError(Exception):
    """The API did not return an answer. Not the model's fault, so never graded as a fail."""


def call_model(base_url, api_key, model, question, temperature):
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{SCHEMA_DOC}\nQuestion: {question}"},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    delay = 5
    for attempt in range(MAX_TRIES):
        last_try = attempt == MAX_TRIES - 1
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            return data["choices"][0]["message"].get("content") or ""
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or last_try:
                raise ModelError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            if last_try:
                raise ModelError(f"{type(exc).__name__}: {exc}") from exc
        time.sleep(delay)
        delay = min(delay * 2, 60)


def cmd_selftest(_args):
    """Check the harness and the tasks without calling any model."""
    failures = []

    def check(name, ok, detail=""):
        print(f"{'ok  ' if ok else 'FAIL'} {name}{'  ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    # Each guard must fail for its own reason; a connection error must not count as a pass.
    _, err = run_sql("DELETE FROM transactions")
    check("writes are rejected", "read-only" in (err or ""), err or "DELETE succeeded")
    _, err = run_sql("SELECT * FROM expected_issues")
    check("answer key is not readable", "permission denied" in (err or ""), err or "SELECT succeeded")
    _, err = run_sql("SELECT pg_sleep(30)")
    check("slow queries time out", "statement timeout" in (err or ""), err or "no timeout")

    for task in TASKS:
        rows, err = run_sql(task["reference"])
        check(f"{task['id']}: reference runs", err is None and len(rows) > 0, err or f"{len(rows)} rows")
        if err:
            continue
        passed, reason = grade(task, task["reference"])
        check(f"{task['id']}: reference passes", passed, reason)
        passed, reason = grade(task, task["naive"])
        check(f"{task['id']}: naive query fails", not passed, reason)

    print(f"\n{len(TASKS)} tasks, {len(failures)} failed checks")
    return 1 if failures else 0


def cmd_run(args):
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("Set LLM_API_KEY in the environment.", file=sys.stderr)
        return 2
    results = []
    for task in TASKS:
        attempts = []
        for _ in range(args.runs):
            try:
                sql = extract_sql(call_model(args.base_url, api_key, args.model, task["question"], args.temperature))
            except ModelError as exc:
                attempts.append({"status": "api_error", "reason": str(exc), "sql": ""})
                continue
            passed, reason = grade(task, sql)
            attempts.append({"status": "pass" if passed else "fail", "reason": reason, "sql": sql})
        n_pass = sum(a["status"] == "pass" for a in attempts)
        n_err = sum(a["status"] == "api_error" for a in attempts)
        fails = [a["reason"] for a in attempts if a["status"] == "fail"]
        print(f"{n_pass}/{len(attempts) - n_err} {task['id']}"
              f"{f'  ({n_err} API errors)' if n_err else ''}{'  ' + fails[0] if fails else ''}", flush=True)
        results.append({"id": task["id"], "traps": task["traps"], "attempts": attempts})

    every = [a for r in results for a in r["attempts"]]
    n_pass = sum(a["status"] == "pass" for a in every)
    n_err = sum(a["status"] == "api_error" for a in every)
    n_solid = sum(
        1 for r in results
        if any(a["status"] != "api_error" for a in r["attempts"])
        and all(a["status"] != "fail" for a in r["attempts"])
    )
    print(f"\n{args.model}: {n_pass}/{len(every) - n_err} graded attempts passed, {n_err} API errors, "
          f"{n_solid}/{len(results)} tasks passed on every graded attempt")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", args.model) + ".json")
    out_file.write_text(json.dumps({
        "model": args.model,
        "base_url": args.base_url,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs_per_task": args.runs,
        "temperature": args.temperature,
        "attempts_passed": n_pass,
        "attempts_graded": len(every) - n_err,
        "api_errors": n_err,
        "tasks_always_passed": n_solid,
        "tasks": len(results),
        "results": results,
    }, indent=2) + "\n")
    print(f"wrote {out_file}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest", help="check the harness and tasks; no model needed")
    run = sub.add_parser("run", help="ask a model every question and grade its SQL")
    run.add_argument("--model", required=True, help="model id as the API expects it")
    run.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
                     help="any OpenAI-compatible chat completions endpoint")
    run.add_argument("--runs", type=int, default=1, help="attempts per question")
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--out", default="results", help="directory for the result file")
    args = parser.parse_args()
    return {"selftest": cmd_selftest, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
