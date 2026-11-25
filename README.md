# txn-dq-checks

[![CI](https://github.com/faceWang753/txn-dq-checks/actions/workflows/ci.yml/badge.svg)](https://github.com/faceWang753/txn-dq-checks/actions/workflows/ci.yml)

Financial data fails quietly. A replayed message, a UTC date used as a business date, or a reversal booked to the wrong account all pass schema validation, and the first sign of trouble is a report that doesn't reconcile.

This repository has five SQL checks that catch those problems, plus a synthetic "trap database" of January 2026 transactions with 26 planted errors and the list of rows each check should flag. Everything runs on PostgreSQL and MySQL 8, and `tests/verify.sql` proves each check finds every planted error and nothing else.

**Why this matters for AI agents.** The same setup is a verifier for agents that write SQL against enterprise data. An agent's query can be run against the trap database and its output compared with the known answers, with no human or LLM judge involved. That makes it usable both as an evaluation suite and as a verifiable reward signal when post-training an agent to work reliably with real databases, which is where this project is headed (see [Roadmap](#roadmap)).

| Check | View | What it catches |
|---|---|---|
| Duplicate postings | `dq_duplicate_postings` | Same upstream `external_ref` loaded twice |
| Precision drift | `dq_precision_drift` | More decimal places than the currency allows (CAD 2, JPY 0) |
| Time-zone value date | `dq_value_date_timezone` | `value_date` taken from the UTC date instead of the Toronto date |
| Broken reversals | `dq_broken_reversals` | Reversal of a missing transaction, wrong account, or amount that doesn't net to zero |
| Late arrivals | `dq_late_arrivals` | Row loaded after that day's report was already generated |

## Quick start (Docker)

```bash
docker compose up -d
```

PostgreSQL:

```bash
docker compose exec postgres psql -U postgres -d txn_dq -f /work/tests/verify.sql
```

```bash
docker compose exec postgres psql -U postgres -d txn_dq -f /work/sql/postgres/04_run_checks.sql
```

MySQL:

```bash
docker compose exec mysql sh -c "mysql -uroot -plocaldev txn_dq < /work/tests/verify.sql"
```

```bash
docker compose exec mysql sh -c "mysql -uroot -plocaldev txn_dq < /work/sql/mysql/04_run_checks.sql"
```

`verify.sql` passes when it returns **zero rows**. Any row is either a planted problem that was missed or a clean row that was wrongly flagged.

## Without Docker

Run these in order against an empty database (`<db>` is `postgres` or `mysql`):

1. `sql/<db>/01_schema.sql`
2. `data/seed.sql`
3. `tests/expected_issues.sql`
4. `sql/<db>/03_checks.sql`
5. `tests/verify.sql`, which should return no rows
6. `sql/<db>/04_run_checks.sql`, which writes to `dq_results` and prints counts per check

## Grading SQL written by a model

`eval/` holds 12 natural-language questions over the trap database and a grader that runs a model's SQL and compares its result set with a reference query's. PostgreSQL only for now.

- Each question says exactly what to compute and which columns to return, so there is one right answer. The model still has to notice, for example, that "Toronto calendar date" is not `value_date`, or that `NOT IN` over a nullable column returns nothing.
- Model SQL runs as the role `agent_ro`, which can only read the three data tables. Every query runs in a read-only transaction that is rolled back, with a 5-second timeout.
- Result sets are compared as multisets of rows: row order and column names are ignored; column order, duplicates and values are not.
- Each task also carries a plausible wrong query. The self-test checks that it fails, which proves the task can tell a right answer from a wrong one on this data.

Check the harness and tasks (no model or API key needed):

```bash
python eval/harness.py selftest
```

Grade a model through any OpenAI-compatible endpoint (OpenRouter by default). Put the key in the `LLM_API_KEY` environment variable:

```bash
python eval/harness.py run --model <model-id>
```

Results are written to `results/<model-id>.json` with the SQL the model wrote for each question. Add `--runs 3` to ask each question three times.

First results for two open-weight models, with the queries that failed and why, are in [RESULTS.md](RESULTS.md).

What this does not cover yet: the model gets one attempt per question with the schema in the prompt and cannot explore the database; nothing is graded on MySQL; and 12 tasks is too few to rank models with any confidence.

## Letting an agent query as a named user

`north_mcp/` is an MCP server for Cohere North that runs an agent's SQL inside the database entitlement of the person asking, with default deny, row-level security, one read-only statement per call and an audit log. It comes with tests that prove each guard blocks what it claims to. See [north_mcp/README.md](north_mcp/README.md).

## The sample data

- 641 transactions for January 2026. All timestamps are UTC; `value_date` is the business date in America/Toronto.
- 615 clean rows, including correct reversals and correct evening postings (after midnight UTC but still the previous day in Toronto), so each check is tested against false positives as well.
- 26 rows with planted problems, `txn_id` 701–744, listed in `tests/expected_issues.sql`.

## PostgreSQL vs MySQL differences that matter here

| Topic | PostgreSQL | MySQL 8 |
|---|---|---|
| Time-zone conversion | `AT TIME ZONE 'America/Toronto'` works out of the box | `CONVERT_TZ` with a named zone returns **NULL** unless the time zone tables are loaded, and the check then silently finds nothing. The sample uses a fixed `-05:00` offset |
| Storing UTC timestamps | `TIMESTAMP` stores the value as given | `TIMESTAMP` converts using the session time zone; this repo uses `DATETIME` |
| Current UTC time | `CURRENT_TIMESTAMP AT TIME ZONE 'UTC'` | `UTC_TIMESTAMP()` |
| Full outer join | Supported | Not supported; `verify.sql` uses two anti-joins |

## Limitations

- **Duplicates are matched only on `external_ref`.** A replay that arrives with a new reference (for example, after an upstream retry that regenerates IDs) is not caught. Catching it needs a fuzzy rule on account, amount and time window, which trades misses for false positives.
- **MySQL uses a fixed UTC-5 offset.** That is correct for the January sample data only. Real data that spans daylight-saving changes needs `America/Toronto` with MySQL's time zone tables loaded.
- **Checks scan the whole table.** That is fine for a sample, but a production table needs incremental runs over newly loaded rows, keyed on `loaded_at`.
- **Late arrivals are detected, not fixed.** The check says which reports are now wrong; restating those reports is a separate process.
- **The data is synthetic.** The planted errors are modelled on common failure modes in banking data pipelines, but real data will contain failure modes this set does not cover.

## Roadmap

1. **Agent tasks.** Done for a first set of 12; see `eval/tasks.py`. Next: more tasks per trap and a MySQL variant.
2. **Execution-based grader.** Done; see `eval/harness.py`.
3. **Baselines.** Measure several models with prompting alone, and record which traps each one falls into.
4. **Post-training.** Use the grader as a verifiable reward to fine-tune a small open model, and measure whether it improves on held-out traps without regressing on clean data.

## License

MIT. Copyright (c) 2026 Mingyang (Ethan) Wang. See [LICENSE](LICENSE).
