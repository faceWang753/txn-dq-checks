-- PostgreSQL 14+
-- Deliberately loose, like a real landing table: no UNIQUE on external_ref and no
-- foreign key on reversal_of. The checks in 03_checks.sql catch what the schema does not.

CREATE TABLE currencies (
    code        CHAR(3)  PRIMARY KEY,
    minor_units SMALLINT NOT NULL            -- decimal places the currency allows (CAD 2, JPY 0)
);

CREATE TABLE transactions (
    txn_id       BIGINT         PRIMARY KEY,
    external_ref VARCHAR(40)    NOT NULL,    -- id from the upstream system; should be unique per posting
    account_id   VARCHAR(20)    NOT NULL,
    currency     CHAR(3)        NOT NULL REFERENCES currencies (code),
    amount       NUMERIC(18, 4) NOT NULL,    -- 4 places because upstream sends floats; see precision check
    posted_at    TIMESTAMP      NOT NULL,    -- UTC
    value_date   DATE           NOT NULL,    -- business date in America/Toronto
    reversal_of  BIGINT         NULL,        -- txn_id this row reverses
    loaded_at    TIMESTAMP      NOT NULL     -- UTC, when the row reached this table
);

CREATE TABLE report_runs (
    report_date  DATE      PRIMARY KEY,
    generated_at TIMESTAMP NOT NULL          -- UTC
);

CREATE TABLE dq_results (
    result_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_started_at TIMESTAMP    NOT NULL,
    check_name     VARCHAR(40)  NOT NULL,
    txn_id         BIGINT       NOT NULL,
    detail         VARCHAR(200) NOT NULL
);

-- Test fixture: which rows the checks should flag.
CREATE TABLE expected_issues (
    check_name VARCHAR(40) NOT NULL,
    txn_id     BIGINT      NOT NULL
);
