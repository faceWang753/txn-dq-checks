-- PostgreSQL. The role that model-written SQL runs as: it can read the three data tables
-- and nothing else. It cannot write, and it cannot read expected_issues or the dq_* views,
-- which would give the answers away. Throwaway password for local use only.
CREATE ROLE agent_ro LOGIN PASSWORD 'localdev';
GRANT CONNECT ON DATABASE txn_dq TO agent_ro;
GRANT SELECT ON currencies, transactions, report_runs TO agent_ro;
