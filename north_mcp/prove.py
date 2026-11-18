"""Prove the guard blocks what it claims to block, each for its own reason.

    python north_mcp/prove.py

Needs only PostgreSQL from docker compose; no North tenant, no model, no API key.
A check passes only when the query fails with the expected error text, so a connection
problem or a typo can never count as "blocked".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import guard
from tasks import TASKS

CAD = "cad.analyst@example.com"
RISK = "risk.officer@example.com"
STRANGER = "someone.else@example.com"

failures = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'  ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(name)


def blocked(name, email, sql, expected_error):
    result = guard.run_query(email, sql)
    error = result.get("error") or ""
    check(name, result["status"] != "ok" and expected_error in error, error or "query succeeded")


def scalar(email, sql):
    result = guard.run_query(email, sql)
    if result["status"] != "ok":
        raise RuntimeError(f"{sql!r} failed: {result.get('error')}")
    return result["rows"][0][0]


def main():
    audit_before = sum(1 for _ in guard.AUDIT_FILE.open()) if guard.AUDIT_FILE.exists() else 0
    calls = 0

    print("-- identity")
    for label, email in (("unknown user", STRANGER), ("missing identity", None), ("empty identity", "")):
        result = guard.run_query(email, "SELECT 1")
        calls += 1
        check(f"{label} is denied before the database is touched", result["status"] == "denied",
              result.get("error"))

    print("-- row scope")
    risk_cad = scalar(RISK, "SELECT count(*) FROM transactions WHERE currency = 'CAD'")
    risk_all = scalar(RISK, "SELECT count(*) FROM transactions")
    desk_all = scalar(CAD, "SELECT count(*) FROM transactions")
    desk_other = scalar(CAD, "SELECT count(*) FROM transactions WHERE currency <> 'CAD'")
    desk_distinct = guard.run_query(CAD, "SELECT DISTINCT currency FROM transactions")["rows"]
    calls += 5
    check("risk role sees more than CAD", risk_all > risk_cad > 0, f"{risk_all} rows, {risk_cad} CAD")
    check("CAD desk sees every CAD row", desk_all == risk_cad, f"{desk_all} of {risk_cad}")
    check("CAD desk sees no other currency", desk_other == 0 and desk_distinct == [["CAD"]],
          f"{desk_other} rows, currencies {desk_distinct}")
    same_question = "SELECT sum(amount) FROM transactions"
    check("same question, different answer per user", scalar(CAD, same_question) != scalar(RISK, same_question))
    calls += 2

    print("-- attempts to widen scope")
    blocked("SET ROLE to the wider role", CAD, "SET ROLE north_risk_all", "permission denied")
    blocked("set_config('role') inside a SELECT", CAD,
            "SELECT set_config('role', 'north_risk_all', false)", "permission denied")
    blocked("second statement after RESET ROLE", CAD,
            "RESET ROLE; SELECT count(*) FROM transactions", "multiple commands")
    calls += 3

    print("-- guards")
    blocked("writes are rejected", RISK, "DELETE FROM transactions", "read-only")
    blocked("COMMIT then write is rejected", RISK, "COMMIT; DELETE FROM transactions", "multiple commands")
    blocked("COMMIT then slow query is rejected", RISK, "COMMIT; SELECT pg_sleep(30)", "multiple commands")
    blocked("slow queries time out", RISK, "SELECT pg_sleep(30)", "statement timeout")
    blocked("answer key is not readable", RISK, "SELECT * FROM expected_issues", "permission denied")
    blocked("check results are not readable", RISK, "SELECT * FROM dq_results", "permission denied")
    blocked("password hashes are not readable", RISK, "SELECT * FROM pg_authid", "permission denied")
    blocked("temp tables cannot be created", RISK, "CREATE TEMP TABLE t AS SELECT 1", "read-only")
    calls += 8
    big = guard.run_query(RISK, "SELECT generate_series(1, 100000)")
    calls += 1
    check("large results are capped and flagged",
          big["status"] == "ok" and big["row_count"] == guard.MAX_ROWS and big["truncated"] is True,
          f"{big.get('row_count')} rows, truncated={big.get('truncated')}")

    print("-- still useful")
    ok_refs = 0
    for task in TASKS:
        result = guard.run_query(RISK, task["reference"])
        calls += 1
        ok_refs += result["status"] == "ok" and result["row_count"] > 0
    check("all reference queries from eval/ run for the full-scope user",
          ok_refs == len(TASKS), f"{ok_refs} of {len(TASKS)}")

    print("-- audit")
    lines = guard.AUDIT_FILE.read_text().splitlines()[audit_before:]
    check("every call was audited, including denied ones", len(lines) == calls,
          f"{len(lines)} entries for {calls} calls")
    check("denied calls record who asked", any('"status": "denied"' in line and STRANGER in line for line in lines))

    print(f"\n{len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
