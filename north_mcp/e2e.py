"""End-to-end check: start the server and call it over HTTP the way North would.

    python north_mcp/e2e.py

The identity arrives in the X-North-ID-Token header. These tokens are signed with a
throwaway key, which the server accepts only because NORTH_TRUSTED_ISSUERS is not set.
"""

import asyncio
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import jwt
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:5222"
failures = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'  ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(name)


def client(email):
    headers = {}
    if email:
        headers["X-North-ID-Token"] = jwt.encode({"email": email}, "throwaway-key-for-local-tests-only", "HS256")
    return Client(StreamableHttpTransport(BASE + "/mcp", headers=headers))


async def count_rows(email):
    async with client(email) as c:
        return (await c.call_tool("run_query", {"sql": "SELECT count(*) FROM transactions"})).data


async def run_checks():
    async with client("risk.officer@example.com") as c:
        tools = sorted(t.name for t in await c.list_tools())
        access = (await c.call_tool("describe_access", {})).data
    check("server exposes two tools", tools == ["describe_access", "run_query"], tools)
    check("describe_access reports the caller's role", access.get("role") == "north_risk_all", access.get("role"))

    risk = await count_rows("risk.officer@example.com")
    desk = await count_rows("cad.analyst@example.com")
    stranger = await count_rows("someone.else@example.com")
    check("risk officer gets all rows", risk["status"] == "ok", risk.get("rows"))
    check("CAD analyst gets fewer rows from the same SQL",
          desk["status"] == "ok" and 0 < desk["rows"][0][0] < risk["rows"][0][0], desk.get("rows"))
    check("user with no entitlement is denied", stranger["status"] == "denied", stranger.get("error"))

    # The SDK lets a request with no token through to the tool with an empty identity,
    # so the default deny in guard.py is what keeps the data closed.
    anonymous = await count_rows(None)
    check("request without a token gets no data", anonymous["status"] == "denied", anonymous.get("error"))


def main():
    server = subprocess.Popen([sys.executable, str(HERE / "server.py")], cwd=HERE)
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=1) as resp:
                    check("health endpoint answers without a token", resp.status == 200)
                    break
            except OSError:
                time.sleep(0.5)
        else:
            check("server started", False, "no answer on /health")
            return 1
        asyncio.run(run_checks())
    finally:
        server.terminate()
        server.wait(timeout=10)
    print(f"\n{len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
