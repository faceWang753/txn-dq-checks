-- PostgreSQL: record one run of all checks in the audit table, then summarise it.
-- CURRENT_TIMESTAMP is timestamptz; convert to UTC explicitly before storing in a TIMESTAMP column.

INSERT INTO dq_results (run_started_at, check_name, txn_id, detail)
SELECT CURRENT_TIMESTAMP AT TIME ZONE 'UTC', check_name, txn_id, detail
FROM dq_all_issues;

SELECT check_name, COUNT(*) AS issues
FROM dq_results
WHERE run_started_at = (SELECT MAX(run_started_at) FROM dq_results)
GROUP BY check_name
ORDER BY check_name;
