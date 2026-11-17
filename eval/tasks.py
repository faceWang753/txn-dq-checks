"""Text-to-SQL tasks over the trap database (PostgreSQL).

Each task has:
  question   what the model is asked, including the exact output columns
  traps      which planted problems the question depends on
  reference  a query that gives the correct answer
  naive      a plausible query that falls into the trap; `harness.py selftest`
             checks that it does NOT match the reference, which proves the task
             can tell a right answer from a wrong one on this data
"""

SCHEMA_DOC = """\
Database: PostgreSQL 16. The data covers January 2026 and is known to contain quality problems.

currencies(
  code         CHAR(3) PRIMARY KEY,
  minor_units  SMALLINT          -- decimal places the currency allows
)

transactions(
  txn_id        BIGINT PRIMARY KEY,
  external_ref  VARCHAR(40),     -- id from the upstream system; a replayed message arrives
                                 -- again with the same external_ref under a new txn_id
  account_id    VARCHAR(20),
  currency      CHAR(3),         -- references currencies.code
  amount        NUMERIC(18,4),   -- positive = inflow, negative = outflow
  posted_at     TIMESTAMP,       -- UTC, stored without a time zone
  value_date    DATE,            -- business date as supplied by upstream
  reversal_of   BIGINT NULL,     -- txn_id this row reverses; not a foreign key
  loaded_at     TIMESTAMP        -- UTC, when the row reached this table
)

report_runs(
  report_date   DATE PRIMARY KEY,  -- business date the end-of-day report covers
  generated_at  TIMESTAMP          -- UTC
)
"""

TASKS = [
    {
        "id": "dedupe_by_currency",
        "traps": ["duplicate_posting"],
        "question": (
            "For each currency, return the number of distinct upstream postings and their total "
            "amount. A replayed external_ref must be counted once: keep the copy that was loaded "
            "first. Columns: currency, postings, total_amount."
        ),
        "reference": """
            SELECT currency, COUNT(*) AS postings, SUM(amount) AS total_amount
            FROM (
                SELECT currency, amount,
                       ROW_NUMBER() OVER (PARTITION BY external_ref ORDER BY loaded_at, txn_id) AS copy_no
                FROM transactions
            ) t
            WHERE copy_no = 1
            GROUP BY currency
        """,
        "naive": """
            SELECT currency, COUNT(*), SUM(amount) FROM transactions GROUP BY currency
        """,
    },
    {
        "id": "toronto_daily_cad",
        "traps": ["value_date_timezone"],
        "question": (
            "For CAD rows, return the number of rows and the total amount per Toronto calendar "
            "date of posted_at (posted_at is stored in UTC). Count every row in the table, "
            "including replays. Columns: toronto_date, row_count, total_amount."
        ),
        "reference": """
            SELECT CAST((posted_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Toronto' AS DATE) AS toronto_date,
                   COUNT(*) AS row_count, SUM(amount) AS total_amount
            FROM transactions
            WHERE currency = 'CAD'
            GROUP BY 1
        """,
        "naive": """
            SELECT value_date, COUNT(*), SUM(amount)
            FROM transactions WHERE currency = 'CAD' GROUP BY value_date
        """,
    },
    {
        "id": "wrong_value_date",
        "traps": ["value_date_timezone"],
        "question": (
            "Return the txn_id of every row whose value_date is not the Toronto calendar date of "
            "posted_at (posted_at is stored in UTC). Column: txn_id."
        ),
        "reference": """
            SELECT txn_id
            FROM transactions
            WHERE value_date <> CAST((posted_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Toronto' AS DATE)
        """,
        "naive": """
            SELECT txn_id FROM transactions WHERE value_date <> CAST(posted_at AS DATE)
        """,
    },
    {
        "id": "precision_by_currency",
        "traps": ["precision_drift"],
        "question": (
            "For every currency in the currencies table, return how many transaction rows have an "
            "amount with more decimal places than that currency's minor_units allows. Include "
            "currencies with zero such rows. Columns: currency, bad_rows."
        ),
        "reference": """
            SELECT c.code AS currency, COUNT(t.txn_id) AS bad_rows
            FROM currencies c
            LEFT JOIN transactions t
                   ON t.currency = c.code AND t.amount <> ROUND(t.amount, c.minor_units)
            GROUP BY c.code
        """,
        "naive": """
            SELECT currency, COUNT(*) FROM transactions
            WHERE amount <> ROUND(amount, 2) GROUP BY currency
        """,
    },
    {
        "id": "invalid_reversals",
        "traps": ["orphan_reversal", "reversal_amount_mismatch", "reversal_account_mismatch"],
        "question": (
            "A reversal row (reversal_of is not null) is valid only if the row it points to exists, "
            "is on the same account and currency, and has exactly the opposite amount. Return the "
            "txn_id of every reversal row that is not valid. Column: txn_id."
        ),
        "reference": """
            SELECT r.txn_id
            FROM transactions r
            LEFT JOIN transactions o ON o.txn_id = r.reversal_of
            WHERE r.reversal_of IS NOT NULL
              AND (   o.txn_id IS NULL
                   OR r.account_id <> o.account_id
                   OR r.currency   <> o.currency
                   OR r.amount     <> -o.amount)
        """,
        "naive": """
            SELECT r.txn_id
            FROM transactions r
            JOIN transactions o ON o.txn_id = r.reversal_of
            WHERE r.amount <> -o.amount
        """,
    },
    {
        "id": "validly_reversed_count",
        "traps": ["orphan_reversal", "reversal_amount_mismatch", "reversal_account_mismatch"],
        "question": (
            "How many transactions have been validly reversed? A transaction is validly reversed "
            "when another row's reversal_of points to it and that row is on the same account and "
            "currency with exactly the opposite amount. Return one row. Column: reversed_count."
        ),
        "reference": """
            SELECT COUNT(DISTINCT o.txn_id) AS reversed_count
            FROM transactions r
            JOIN transactions o ON o.txn_id = r.reversal_of
            WHERE r.account_id = o.account_id
              AND r.currency   = o.currency
              AND r.amount     = -o.amount
        """,
        "naive": """
            SELECT COUNT(DISTINCT reversal_of) FROM transactions WHERE reversal_of IS NOT NULL
        """,
    },
    {
        "id": "never_reversed_count",
        "traps": ["null_in_not_in"],
        "question": (
            "How many rows in transactions are not pointed to by any row's reversal_of? "
            "Return one row. Column: not_reversed."
        ),
        "reference": """
            SELECT COUNT(*) AS not_reversed
            FROM transactions t
            WHERE NOT EXISTS (SELECT 1 FROM transactions r WHERE r.reversal_of = t.txn_id)
        """,
        "naive": """
            SELECT COUNT(*) FROM transactions
            WHERE txn_id NOT IN (SELECT reversal_of FROM transactions)
        """,
    },
    {
        "id": "stale_reports",
        "traps": ["late_arrival"],
        "question": (
            "A report for a business date is stale if any transaction with that value_date was "
            "loaded after the report's generated_at. Return each stale report date with the number "
            "of such late rows and their total amount. Columns: report_date, late_rows, late_amount."
        ),
        "reference": """
            SELECT rr.report_date, COUNT(*) AS late_rows, SUM(t.amount) AS late_amount
            FROM transactions t
            JOIN report_runs rr ON rr.report_date = t.value_date
            WHERE t.loaded_at > rr.generated_at
            GROUP BY rr.report_date
        """,
        "naive": """
            SELECT value_date, COUNT(*), SUM(amount)
            FROM transactions
            WHERE CAST(loaded_at AS DATE) > value_date
            GROUP BY value_date
        """,
    },
    {
        "id": "reported_vs_corrected",
        "traps": ["late_arrival"],
        "question": (
            "A report for a business date is stale if any transaction with that value_date was "
            "loaded after the report's generated_at. For each stale report date, return the CAD "
            "total the report would have shown (CAD rows with that value_date loaded at or before "
            "generated_at; 0 if none) and the corrected CAD total (all CAD rows with that "
            "value_date). Columns: report_date, reported_total, corrected_total."
        ),
        "reference": """
            SELECT rr.report_date,
                   COALESCE(SUM(t.amount) FILTER (WHERE t.currency = 'CAD'
                                                    AND t.loaded_at <= rr.generated_at), 0) AS reported_total,
                   COALESCE(SUM(t.amount) FILTER (WHERE t.currency = 'CAD'), 0)             AS corrected_total
            FROM report_runs rr
            JOIN transactions t ON t.value_date = rr.report_date
            GROUP BY rr.report_date, rr.generated_at
            HAVING COUNT(*) FILTER (WHERE t.loaded_at > rr.generated_at) > 0
        """,
        "naive": """
            SELECT rr.report_date,
                   COALESCE(SUM(t.amount) FILTER (WHERE t.currency = 'CAD'
                                                    AND t.posted_at <= rr.generated_at), 0),
                   COALESCE(SUM(t.amount) FILTER (WHERE t.currency = 'CAD'), 0)
            FROM report_runs rr
            JOIN transactions t ON t.value_date = rr.report_date
            GROUP BY rr.report_date, rr.generated_at
            HAVING COUNT(*) FILTER (WHERE t.posted_at > rr.generated_at) > 0
        """,
    },
    {
        "id": "rounded_totals",
        "traps": ["precision_drift"],
        "question": (
            "For each currency, return the total amount after first rounding each row's amount to "
            "the number of decimal places that currency allows (currencies.minor_units). Count "
            "every row in the table. Columns: currency, rounded_total."
        ),
        "reference": """
            SELECT t.currency, SUM(ROUND(t.amount, c.minor_units)) AS rounded_total
            FROM transactions t
            JOIN currencies c ON c.code = t.currency
            GROUP BY t.currency
        """,
        "naive": """
            SELECT currency, ROUND(SUM(amount), 2) FROM transactions GROUP BY currency
        """,
    },
    {
        "id": "inclusive_week",
        "traps": ["timestamp_range"],
        "question": (
            "How many rows have posted_at (UTC) on any day from 5 January 2026 through "
            "11 January 2026 inclusive? Return one row. Column: row_count."
        ),
        "reference": """
            SELECT COUNT(*) AS row_count
            FROM transactions
            WHERE posted_at >= TIMESTAMP '2026-01-05' AND posted_at < TIMESTAMP '2026-01-12'
        """,
        "naive": """
            SELECT COUNT(*) FROM transactions
            WHERE posted_at BETWEEN '2026-01-05' AND '2026-01-11'
        """,
    },
    {
        "id": "toronto_daily_net_deduped",
        "traps": ["duplicate_posting", "value_date_timezone"],
        "question": (
            "Return the net CAD amount per Toronto calendar date of posted_at (posted_at is stored "
            "in UTC), counting a replayed external_ref once: keep the copy that was loaded first. "
            "Columns: toronto_date, net_amount."
        ),
        "reference": """
            SELECT CAST((posted_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Toronto' AS DATE) AS toronto_date,
                   SUM(amount) AS net_amount
            FROM (
                SELECT posted_at, amount, currency,
                       ROW_NUMBER() OVER (PARTITION BY external_ref ORDER BY loaded_at, txn_id) AS copy_no
                FROM transactions
            ) t
            WHERE copy_no = 1 AND currency = 'CAD'
            GROUP BY 1
        """,
        "naive": """
            SELECT value_date, SUM(amount) FROM transactions WHERE currency = 'CAD' GROUP BY value_date
        """,
    },
]
