# Governed SQL for Cohere North

An MCP server that lets a North agent query a database as the person who is asking. Two people can ask the same question and get different, correct answers, because each query runs inside that person's database entitlement. It is built on Cohere's [north-mcp-python-sdk](https://github.com/cohere-ai/north-mcp-python-sdk).

It has not been run inside a North tenant. It is tested against the SDK itself over HTTP, with the identity header North sends.

## How it works

```
North user -> X-North-ID-Token -> email -> access map -> database role -> one read-only statement
```

- **Default deny.** A user who is not in the access map gets no data. So does a request with no identity.
- **The database enforces scope.** Each entitlement group is a PostgreSQL login role with row-level security (`roles.sql`). The server never parses or rewrites the SQL, so there is no filter for a clever query to slip past.
- **One statement per call**, in a read-only transaction that is rolled back, with a 5-second timeout and a 200-row cap.
- **Every call is logged** to `audit.jsonl` with the user, role, SQL, outcome, row count and duration. Denied calls are logged too.
- **Refusals are flagged.** A denied or failed call comes back as an MCP tool error (`isError: true`) with the reason in the text, so a client does not have to parse the payload to know it failed.

The demo has two roles over the trap database in this repository: `north_desk_cad` sees CAD transactions only, and `north_risk_all` sees every currency.

## Run the proofs

Start the databases first with `docker compose up -d`, then install the requirements:

```bash
pip install -r north_mcp/requirements.txt
```

`prove.py` calls the guard directly and needs only PostgreSQL:

```bash
python north_mcp/prove.py
```

`e2e.py` starts the real server and calls it over HTTP as three different users:

```bash
python north_mcp/e2e.py
```

A check passes only when the query fails with the expected error text, so a connection problem cannot count as "blocked".

| Attempt | Result |
|---|---|
| User not in the access map, or no identity | Denied before the database is touched |
| CAD analyst runs `SELECT count(*) FROM transactions` | 505 rows; the risk officer gets 641 from the same SQL |
| `SET ROLE north_risk_all` | `permission denied to set role` |
| `SELECT set_config('role', 'north_risk_all', false)` | `permission denied to set role` |
| `RESET ROLE; SELECT ...` or `COMMIT; DELETE ...` | `cannot insert multiple commands into a prepared statement` |
| `DELETE FROM transactions` | `cannot execute DELETE in a read-only transaction` |
| `SELECT pg_sleep(30)` | `canceling statement due to statement timeout` |
| Read the answer key or `pg_authid` | `permission denied for table` |
| `SELECT generate_series(1, 100000)` | 200 rows returned, flagged as truncated |

## Two things I got wrong first

**A second statement escaped the timeout.** The grader in `eval/` already ran model SQL in a read-only transaction with a 5-second timeout. But psycopg sends a query without parameters using the simple protocol, which accepts several statements at once. `COMMIT; SELECT pg_sleep(8)` ended the guarded transaction and then ran for 8.3 seconds. The grader's role could still only read, so nothing could be changed, but the timeout was not a real limit. Sending the query with `prepare=True` uses the extended protocol, which refuses more than one statement. Both the grader and this server now do that, and both test it.

**I expected the SDK to reject a request with no token.** With the SDK version installed on 18 November 2026 and default settings, a request with no identity header reached the tool with an empty identity. The server stays closed only because the guard denies any caller it cannot map to a role. Do not build a tool that treats "the call arrived" as "the caller is known".

## Before using this for real

- Set `NORTH_TRUSTED_ISSUERS`. Without it the SDK does not verify the ID token's signature, and anyone who can reach the server can claim any email. The tests here rely on that to forge identities.
- Keep the server off any network North is not on.
- Replace the shared demo password with one secret per role.
- The access map is a JSON file. In a real deployment it would come from the identity provider's groups.
- Each call opens a new connection. That is simple and safe, but it will need pooling per role under load.
- Entitlement by currency is a stand-in. Real desks are scoped by book, legal entity or client.
