-- Runs on PostgreSQL and MySQL. Passes when it returns zero rows:
--   'missed'     = a planted problem no check caught
--   'unexpected' = a check flagged a row that was supposed to be clean (false positive)
-- MySQL has no FULL OUTER JOIN, so this uses two anti-joins.

SELECT 'missed' AS outcome, e.check_name, e.txn_id
FROM expected_issues e
LEFT JOIN dq_all_issues a
       ON a.check_name = e.check_name AND a.txn_id = e.txn_id
WHERE a.txn_id IS NULL

UNION ALL

SELECT 'unexpected' AS outcome, a.check_name, a.txn_id
FROM dq_all_issues a
LEFT JOIN expected_issues e
       ON e.check_name = a.check_name AND e.txn_id = a.txn_id
WHERE e.txn_id IS NULL

ORDER BY outcome, check_name, txn_id;
