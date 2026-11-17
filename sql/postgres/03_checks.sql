-- PostgreSQL 14+
-- Each check is a view returning (txn_id, check_name, detail). One row = one problem.

-- 1. Duplicate postings: the same upstream external_ref loaded more than once.
--    The first copy (earliest loaded_at) is treated as the original; later copies are flagged.
CREATE OR REPLACE VIEW dq_duplicate_postings AS
SELECT txn_id,
       CAST('duplicate_posting' AS VARCHAR(40)) AS check_name,
       CONCAT('external_ref ', external_ref, ' already loaded as txn ', first_txn_id) AS detail
FROM (
    SELECT txn_id,
           external_ref,
           FIRST_VALUE(txn_id) OVER (PARTITION BY external_ref ORDER BY loaded_at, txn_id) AS first_txn_id,
           ROW_NUMBER()        OVER (PARTITION BY external_ref ORDER BY loaded_at, txn_id) AS copy_no
    FROM transactions
) d
WHERE copy_no > 1;

-- 2. Precision drift: more decimal places than the currency allows.
CREATE OR REPLACE VIEW dq_precision_drift AS
SELECT t.txn_id,
       CAST('precision_drift' AS VARCHAR(40)) AS check_name,
       CONCAT(t.currency, ' amount ', t.amount, ' has more than ', c.minor_units, ' decimal places') AS detail
FROM transactions t
JOIN currencies c ON c.code = t.currency
WHERE t.amount <> ROUND(t.amount, c.minor_units);

-- 3. Value date does not match the Toronto calendar date of posted_at.
--    Typical cause: someone took the date of the UTC timestamp.
CREATE OR REPLACE VIEW dq_value_date_timezone AS
SELECT txn_id,
       CAST('value_date_timezone' AS VARCHAR(40)) AS check_name,
       CONCAT('value_date ', value_date, ' but Toronto date of posted_at is ', toronto_date) AS detail
FROM (
    SELECT txn_id,
           value_date,
           CAST((posted_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Toronto' AS DATE) AS toronto_date
    FROM transactions
) x
WHERE value_date <> toronto_date;

-- 4. Broken reversals: the original is missing, or the pair does not net to zero on one account.
CREATE OR REPLACE VIEW dq_broken_reversals AS
SELECT r.txn_id,
       CAST(CASE
           WHEN o.txn_id IS NULL                                  THEN 'orphan_reversal'
           WHEN r.account_id <> o.account_id                      THEN 'reversal_account_mismatch'
           ELSE                                                        'reversal_amount_mismatch'
       END AS VARCHAR(40)) AS check_name,
       CONCAT('reverses txn ', r.reversal_of) AS detail
FROM transactions r
LEFT JOIN transactions o ON o.txn_id = r.reversal_of
WHERE r.reversal_of IS NOT NULL
  AND (   o.txn_id IS NULL
       OR r.account_id <> o.account_id
       OR r.currency   <> o.currency
       OR r.amount     <> -o.amount);

-- 5. Late arrivals: loaded after the report for its value_date was already generated,
--    so that report is now wrong.
CREATE OR REPLACE VIEW dq_late_arrivals AS
SELECT t.txn_id,
       CAST('late_arrival' AS VARCHAR(40)) AS check_name,
       CONCAT('report for ', t.value_date, ' generated ', rr.generated_at, ', row loaded ', t.loaded_at) AS detail
FROM transactions t
JOIN report_runs rr ON rr.report_date = t.value_date
WHERE t.loaded_at > rr.generated_at;

CREATE OR REPLACE VIEW dq_all_issues AS
SELECT txn_id, check_name, detail FROM dq_duplicate_postings
UNION ALL SELECT txn_id, check_name, detail FROM dq_precision_drift
UNION ALL SELECT txn_id, check_name, detail FROM dq_value_date_timezone
UNION ALL SELECT txn_id, check_name, detail FROM dq_broken_reversals
UNION ALL SELECT txn_id, check_name, detail FROM dq_late_arrivals;
