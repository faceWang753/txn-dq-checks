-- MySQL: record one run of all checks in the audit table, then summarise it.
-- UTC_TIMESTAMP() rather than NOW(), so the audit time is UTC like the rest of the data.

INSERT INTO dq_results (run_started_at, check_name, txn_id, detail)
SELECT UTC_TIMESTAMP(), check_name, txn_id, detail
FROM dq_all_issues;

SELECT check_name, COUNT(*) AS issues
FROM dq_results
WHERE run_started_at = (SELECT MAX(run_started_at) FROM dq_results)
GROUP BY check_name
ORDER BY check_name;
