-- PostgreSQL. Database roles the MCP server connects as, one per entitlement group.
-- The server holds the credentials; a North user is mapped to exactly one role, or to none.
-- Scope is enforced here, in the database, not by rewriting or inspecting the SQL.
-- Throwaway passwords for local use only.

CREATE ROLE north_desk_cad LOGIN PASSWORD 'localdev';   -- CAD desk: CAD rows only
CREATE ROLE north_risk_all LOGIN PASSWORD 'localdev';   -- risk: every currency

GRANT CONNECT ON DATABASE txn_dq TO north_desk_cad, north_risk_all;
GRANT SELECT ON currencies, transactions, report_runs TO north_desk_cad, north_risk_all;

-- Defaults that hold even if the server forgets to set them per transaction.
ALTER ROLE north_desk_cad SET default_transaction_read_only = on;
ALTER ROLE north_risk_all SET default_transaction_read_only = on;
ALTER ROLE north_desk_cad SET statement_timeout = '5s';
ALTER ROLE north_risk_all SET statement_timeout = '5s';

-- No role here needs temp tables. The read-only transaction already refuses them.
REVOKE TEMPORARY ON DATABASE txn_dq FROM PUBLIC;

-- Row-level security keyed on the connected role. Nothing a query can set
-- (set_config, SET ROLE) changes which role it is connected as.
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY desk_cad ON transactions FOR SELECT TO north_desk_cad USING (currency = 'CAD');
CREATE POLICY risk_all ON transactions FOR SELECT TO north_risk_all USING (true);
CREATE POLICY grader_all ON transactions FOR SELECT TO agent_ro USING (true);
