# First results (17 November 2026)

Two open-weight models of the same size class (30B parameters, 3B active) answered the 12 questions in `eval/tasks.py`. Each model got one attempt per question at temperature 0, with the schema in the prompt and no access to the database. Both were called through OpenRouter's free endpoints. The SQL each model wrote is in `results/`.

| Model | Passed |
|---|---|
| `cohere/north-mini-code:free` | 9 / 12 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 11 / 12 |

**This is not a ranking.** With 12 questions and one attempt each, a two-question gap is within what a rerun could change. A third model, `google/gemma-4-26b-a4b-it:free`, was rate-limited on every call and has no score.

## Per question

| Question | Trap | North Mini Code | Nemotron 3 Nano |
|---|---|---|---|
| `dedupe_by_currency` | replayed postings | pass | pass |
| `toronto_daily_cad` | UTC vs Toronto date | pass | pass |
| `wrong_value_date` | UTC vs Toronto date | **fail** | pass |
| `precision_by_currency` | currency decimals | **fail** | **fail** |
| `invalid_reversals` | broken reversals | pass | pass |
| `validly_reversed_count` | broken reversals | pass | pass |
| `never_reversed_count` | `NOT IN` over NULLs | pass | pass |
| `stale_reports` | late arrivals | pass | pass |
| `reported_vs_corrected` | late arrivals | pass | pass |
| `rounded_totals` | currency decimals | **fail** | pass |
| `inclusive_week` | timestamp range | pass | pass |
| `toronto_daily_net_deduped` | replays + Toronto date | pass | pass |

## What went wrong

**North Mini Code, `wrong_value_date`: time zone converted in the wrong direction.** The query returned 213 rows; the answer is 5.

```sql
SELECT txn_id FROM transactions
WHERE value_date <> DATE(timezone('America/Toronto', posted_at));
```

`posted_at` is a `TIMESTAMP` without a time zone. Applied to that type, `timezone('America/Toronto', ...)` reads the value as Toronto local time and converts it to UTC, which is the opposite of what the question needs. The same model converted correctly in the two aggregate questions that use the Toronto date, so the knowledge is there but is not applied consistently.

**North Mini Code, `precision_by_currency`: a function that does not exist.** The query called `numeric_scale(t.amount)`. PostgreSQL has `scale()`, not `numeric_scale()`, so the query failed to run.

**North Mini Code, `rounded_totals`: no aggregation.** The question asks for one total per currency. The query selected `ROUND(amount, currencies.minor_units)` per row with no `SUM` or `GROUP BY`, and referred to the table as `currencies` after aliasing it as `c`, which is an error.

**Nemotron 3 Nano, `precision_by_currency`: wrong rows.** The query ran and returned three rows, but none of them matched the expected answer.

`precision_by_currency` is the only question both models failed. It needs the row's decimal places compared with a per-currency limit from another table (CAD and USD 2, JPY 0); a fixed `ROUND(amount, 2)` misses the JPY rows.

## Reproduce

```bash
docker compose up -d
```

```bash
python eval/harness.py run --model cohere/north-mini-code:free
```

`LLM_API_KEY` must hold an OpenRouter key. Free endpoints are slow and rate-limited; the harness retries, and reports calls that never returned as API errors instead of counting them as wrong answers.

## Limits of this run

- One attempt per question. The harness now supports `--runs N`; a three-attempt rerun is the next step.
- 12 questions, each built around a known trap. Passing them says little about ordinary queries.
- PostgreSQL only.
- Single-shot: the model cannot inspect the data or retry after an error, which an agent normally could.
- These files were produced before retry and `--runs` were added to the harness, so they use the older one-result-per-question format.
