#!/usr/bin/env python3
"""End-to-end verification of the SAP Business One Service Layer connector.

    python3 tests/verify_b1.py

Boots a mock Service Layer (uvicorn) plus the B1 MCP server (streamable HTTP), then proves:
  client level  -> login refused on bad credentials, session cookie auth, automatic re-login
                   after the session is dropped, $filter/$select/$orderby/$top/$skip, $count,
                   single-key read, 404 and error-envelope parsing, the write gate, real
                   POST/PATCH with independent read-back, duplicate rejection, field whitelist
  tools level   -> every tool returns a JSON-safe dict and never raises
  agent level   -> MCP tools/list + tools/call over raw JSON-RPC

Exit code 0 = every check passed. Logs: /tmp/sap-b1-verify-*.log
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


import httpx  # noqa: E402

from sap_b1 import tools as b1t  # noqa: E402
from sap_b1.client import B1Client, B1Error, B1SessionError, B1WriteBlocked  # noqa: E402
from sap_b1.config import B1Config  # noqa: E402


def _free_port(preferred: int) -> int:
    """Return `preferred` if free, else an ephemeral port.

    Guards the classic trap: if the default port is already taken by some other
    running service, the test silently talks to *that* server and fails with a
    confusing assertion instead of a clear "port busy".
    """
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            with socket.socket() as alt:
                alt.bind(("127.0.0.1", 0))
                return int(alt.getsockname()[1])


USER, PASSWORD, COMPANY = "manager", "mock-pass", "SBODEMO"
MOCK_PORT = _free_port(int(os.environ.get("VERIFY_B1_MOCK_PORT", "8794")))
MCP_PORT = _free_port(int(os.environ.get("VERIFY_B1_MCP_PORT", "8795")))
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"

RESULTS: list[tuple[str, bool, str]] = []


def cfg(**over: Any) -> B1Config:
    values: dict[str, Any] = dict(label="mock-b1", base_url=MOCK_BASE, company_db=COMPANY,
                                  username=USER, password=PASSWORD)
    values.update(over)
    return B1Config(**values)


def check(name: str, fn: Callable[[], Optional[str]]) -> None:
    started = time.time()
    try:
        detail = fn()
        RESULTS.append((name, True, detail or ""))
    except AssertionError as exc:
        RESULTS.append((name, False, f"ASSERT: {exc}"))
    except Exception as exc:                                  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
    finally:
        took = time.time() - started
        if took > 5:
            RESULTS[-1] = (RESULTS[-1][0], RESULTS[-1][1], RESULTS[-1][2] + f" [{took:.1f}s]")


# ------------------------------------------------------------------ JSON-RPC client
class McpHttp:
    def __init__(self, url: str) -> None:
        self.url = url
        self.session: Optional[str] = None
        self.version = "2025-06-18"
        self.http = httpx.Client(timeout=25.0)

    def _post(self, payload: dict[str, Any], notify: bool = False) -> dict[str, Any]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "mcp-protocol-version": self.version}
        if self.session:
            headers["mcp-session-id"] = self.session
        resp = self.http.post(self.url, json=payload, headers=headers)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session = sid
        if notify:
            return {}
        if resp.status_code >= 400:
            raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise AssertionError(f"no data event in SSE body: {resp.text[:160]}")
        return resp.json()

    def initialize(self) -> dict[str, Any]:
        res = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": self.version, "capabilities": {},
                                     "clientInfo": {"name": "verify-b1", "version": "0.1"}}})
        self.version = res.get("result", {}).get("protocolVersion", self.version)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
        return res

    def call(self, method: str, params: Optional[dict[str, Any]] = None,
             rid: int = 2) -> dict[str, Any]:
        res = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if "error" in res:
            raise AssertionError(f"JSON-RPC error: {res['error']}")
        return res["result"]

    def tools(self) -> list[str]:
        return [t["name"] for t in self.call("tools/list", {}, 2).get("tools", [])]

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.call("tools/call", {"name": name, "arguments": args}, 3)
        if result.get("isError"):
            raise AssertionError(f"tool {name} returned isError: {result}")
        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        if not texts:
            raise AssertionError(f"tool {name} returned no text content: {result}")
        return json.loads(texts[0])


# ------------------------------------------------------------------ infrastructure
def start(cmd: list[str], env: dict[str, str], log_file: str) -> subprocess.Popen:
    handle = open(log_file, "w")                                      # noqa: SIM115
    return subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, **env},
                            stdout=handle, stderr=subprocess.STDOUT)


def wait_http(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last: Optional[str] = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=3.0)
            if resp.status_code < 500:
                return
            last = f"status {resp.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.4)
    raise RuntimeError(f"{url} never came up ({last}); see the log file")


def wait_port(host: str, port: int, timeout: float = 30.0) -> None:
    """TCP readiness: a GET on an MCP streamable endpoint opens an SSE stream that never ends."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.4)
    raise RuntimeError(f"{host}:{port} never accepted a connection")


# ------------------------------------------------------------------------ the checks
def main() -> int:
    os.environ.update({
        "SAP_B1_BASE_URL": MOCK_BASE, "SAP_B1_COMPANY_DB": COMPANY,
        "SAP_B1_USERNAME": USER, "SAP_B1_PASSWORD": PASSWORD,
        "SAP_B1_PATH": "/b1s/v1", "SAP_B1_LABEL": "mock-b1",
        "SAP_B1_WRITE_ENABLED": "1",
    })
    for stale in ("SAP_B1_PROFILE",):
        os.environ.pop(stale, None)

    mock = start([sys.executable, "-m", "uvicorn", "mock_b1.server:app", "--host",
                  "127.0.0.1", "--port", str(MOCK_PORT), "--log-level", "warning"],
                 {}, "/tmp/sap-b1-verify-mock.log")
    mcp_proc: Optional[subprocess.Popen] = None
    try:
        wait_http(f"{MOCK_BASE}/healthz")
        health = httpx.get(f"{MOCK_BASE}/healthz", timeout=5).json()
        assert health["ok"] and "BusinessPartners" in health["collections"], health

        def mock_health() -> str:
            assert health["collections"] == sorted(["BusinessPartners", "Items", "Invoices",
                                                    "Orders", "PurchaseOrders"]), \
                health["collections"]
            return f"mock up, collections={len(health['collections'])}"

        check("mock Service Layer is up with the expected collections", mock_health)

        # ---- auth ---------------------------------------------------------
        def bad_login() -> str:
            client = B1Client(cfg(password="wrong"))
            try:
                client.login()
                raise AssertionError("login with a wrong password was accepted")
            except B1SessionError as exc:
                assert exc.status == 401, exc.status
                assert exc.code == -1101, exc.code
                return f"401 / code {exc.code}: {str(exc)[:44]}"
            finally:
                client.close()

        check("login: wrong password refused with the SAP error envelope", bad_login)

        def unconfigured() -> str:
            client = B1Client(B1Config())
            try:
                client.login()
                raise AssertionError("login ran without configuration")
            except B1SessionError as exc:
                assert "SAP_B1_BASE_URL" in str(exc), str(exc)
                return "unconfigured connector refuses to login and names the missing env vars"
            finally:
                client.close()

        check("login: unconfigured connector fails closed", unconfigured)

        def session_and_relogin() -> str:
            client = B1Client(cfg())
            try:
                page = client.query("business_partner", top=2, select="CardCode,CardName")
                assert page["row_count"] == 2, page
                client._http.cookies.clear()                 # simulate an expired session
                client._logged_in = True
                again = client.query("business_partner", top=1)
                assert again["row_count"] == 1, again
                return "cookie dropped -> 401 from SAP -> automatic re-login and retry"
            finally:
                client.close()

        check("session: cookie auth plus one automatic re-login", session_and_relogin)

        # ---- reads --------------------------------------------------------
        def filters_and_select() -> str:
            client = B1Client(cfg())
            try:
                page = client.query("business_partner", filter="CardType eq 'cSupplier'",
                                    select="CardCode,CardName,CardType", top=50, count=True)
                rows = page["items"]
                assert page["count"] == 5 and page["count_scope"] == "filtered", page["count"]
                assert len(rows) == 5, len(rows)
                assert client.count("business_partner") == 17, client.count("business_partner")
                assert all(r["CardType"] == "cSupplier" for r in rows), rows[:2]
                assert set(rows[0]) <= {"CardCode", "CardName", "CardType"}, rows[0]
                sa = client.query("item", filter="contains(ItemName,'Gas')", top=5)
                assert sa["row_count"] == 1, sa["items"]
                return (f"cSupplier filtered count=5, collection count=17, $select honoured, "
                        f"contains() matched {sa['items'][0]['ItemCode']}")
            finally:
                client.close()

        check("reads: $filter + $select + contains() + $count endpoint", filters_and_select)

        def paging() -> str:
            client = B1Client(cfg())
            try:
                p1 = client.query("business_partner", top=4, orderby="CardCode")
                p2 = client.query("business_partner", top=4, skip=4, orderby="CardCode",
                                  count=True)
                keys1 = [r["CardCode"] for r in p1["items"]]
                keys2 = [r["CardCode"] for r in p2["items"]]
                assert len(keys1) == 4 and len(keys2) == 4, (keys1, keys2)
                assert not set(keys1) & set(keys2), keys1
                assert p2["next"] == {"skip": 8}, p2["next"]
                return f"{keys1[0]}..{keys1[-1]} then {keys2[0]}..{keys2[-1]}, count={p2['count']}"
            finally:
                client.close()

        check("reads: $top/$skip paging without overlap + next-page cursor", paging)

        def single_read() -> str:
            client = B1Client(cfg())
            try:
                one = client.get_entity("business_partner", "C0001")
                assert one["item"]["CardCode"] == "C0001", one
                assert one["item"]["CardName"].startswith("Gulf"), one["item"]
                try:
                    client.get_entity("business_partner", "C9999")
                    raise AssertionError("missing business partner did not 404")
                except B1Error as exc:
                    assert exc.status == 404 and exc.code == -5002, (exc.status, exc.code)
                return f"{one['item']['CardCode']} {one['item']['CardName'][:22]} / 404 surfaced"
            finally:
                client.close()

        check("reads: single-key read by CardCode + 404 error envelope", single_read)

        # ---- governance gate + writes -------------------------------------
        def gate_closed() -> str:
            client = B1Client(cfg(write_enabled=False))
            try:
                try:
                    client.create_entity("business_partner",
                                         {"CardCode": "C9001", "CardName": "Gate Test",
                                          "CardType": "cCustomer"}, dry_run=False)
                    raise AssertionError("a write left the box with the gate closed")
                except B1WriteBlocked as exc:
                    assert "SAP_B1_WRITE_ENABLED" in str(exc), str(exc)
                try:
                    client.update_entity("business_partner", "C0001",
                                         {"CardName": "Should Not Apply"}, dry_run=False)
                    raise AssertionError("a PATCH left the box with the gate closed")
                except B1WriteBlocked:
                    pass
                same = client.get_entity("business_partner", "C0001")
                assert same["item"]["CardName"].startswith("Gulf"), same["item"]["CardName"]
                return "POST and PATCH both blocked; C0001 unchanged"
            finally:
                client.close()

        check("governance: write gate blocks POST and PATCH (no request sent)", gate_closed)

        def dry_run_open() -> str:
            client = B1Client(cfg(write_enabled=True))
            try:
                preview = client.create_entity("business_partner",
                                               {"CardCode": "C9001", "CardName": "Dry Run Ltd",
                                                "CardType": "cCustomer"}, dry_run=True)
                assert preview["executed"] is False and preview["preview"]["method"] == "POST"
                try:
                    client.get_entity("business_partner", "C9001")
                    raise AssertionError("dry run actually wrote to SAP")
                except B1Error as exc:
                    assert exc.status == 404, exc.status
                missing = None
                try:
                    client.create_entity("business_partner", {"CardCode": "C9002"},
                                         dry_run=True)
                except B1Error as exc:
                    missing = str(exc)
                assert missing and "CardName" in missing, missing
                return f"dry run sent nothing (404 on read-back); required-field guard: {missing[:44]}"
            finally:
                client.close()

        check("writes: dry-run previews without sending + required-field guard", dry_run_open)

        def real_create() -> str:
            client = B1Client(cfg(write_enabled=True))
            try:
                made = client.create_entity("business_partner",
                                            {"CardCode": "C9001", "CardName": "Az Test Customer",
                                             "CardType": "cCustomer", "Country": "SA",
                                             "Currency": "SAR", "EmailAddress": "qa@example.com"},
                                            dry_run=False)
                assert made["executed"] is True and made["status"] == 201, made
                back = client.get_entity("business_partner", "C9001")
                assert back["item"]["CardName"] == "Az Test Customer", back["item"]
                dup_code = None
                try:
                    client.create_entity("business_partner",
                                         {"CardCode": "C9001", "CardName": "Dup",
                                          "CardType": "cCustomer"}, dry_run=False)
                except B1Error as exc:
                    assert exc.code == -2028, exc.code
                    dup_code = exc.code
                assert dup_code is not None, "duplicate CardCode was accepted"
                return f"C9001 created (201), read-back OK, duplicate rejected code {dup_code}"
            finally:
                client.close()

        check("writes: real POST + independent read-back + duplicate rejection", real_create)

        def real_update() -> str:
            client = B1Client(cfg(write_enabled=True))
            try:
                patched = client.update_entity("business_partner", "C9001",
                                               {"CardName": "Az Test Customer (renamed)",
                                                "City": "Jeddah"}, dry_run=False)
                assert patched["status"] == 204, patched
                back = client.get_entity("business_partner", "C9001")
                assert back["item"]["CardName"] == "Az Test Customer (renamed)", back["item"]
                assert back["item"]["City"] == "Jeddah", back["item"]
                problems = []
                for bad in ({"CardCode": "C9999"}, {"Balance": 1}, {"NotAField": 1}):
                    try:
                        client.update_entity("business_partner", "C9001", bad, dry_run=False)
                    except B1Error as exc:
                        problems.append(str(exc)[:28])
                assert len(problems) == 3, problems
                return "PATCH 204 + read-back verified; key change/unknown field refused"
            finally:
                client.close()

        check("writes: real PATCH + read-back + field whitelist", real_update)

        # ---- tools layer ---------------------------------------------------
        def tools_layer() -> str:
            health_tool = b1t.b1_health()
            assert health_tool["ok"] and health_tool["reachable"], health_tool
            page = b1t.b1_list_business_partners(top=3, country="SA")
            assert page["ok"] and page["row_count"] == 3, page
            assert all(r["Country"] == "SA" for r in page["items"]), page["items"]
            items = b1t.b1_list_items(top=2)
            assert items["ok"] and items["row_count"] == 2, items
            counts = b1t.b1_count("order")
            assert counts["ok"] and counts["count"] == 8, counts
            order_tool = b1t.b1_list_orders(top=2, card_code="C0001")
            assert order_tool["ok"] and order_tool["row_count"] == 2, order_tool
            bad = b1t.b1_query("not_an_entity")
            assert bad["ok"] is False and "unknown entity" in bad["error"]["message"], bad
            blocked = b1t.b1_create_business_partner("C9100", "Gate Tool",
                                                     config=cfg(write_enabled=False),
                                                     dry_run=False)
            assert blocked["ok"] is False and "SAP_B1_WRITE_ENABLED" in \
                blocked["error"]["message"], blocked
            dry = b1t.b1_create_business_partner("C9100", "Dry Tool", dry_run=True)
            assert dry["ok"] and dry["executed"] is False, dry
            return "health/list/count/query + gate block + dry run all return JSON-safe dicts"
        check("tools layer: dict contract, gate block and dry run", tools_layer)

        # ---- agent layer (MCP) ---------------------------------------------
        mcp_env = {"SAP_B1_BASE_URL": MOCK_BASE, "SAP_B1_COMPANY_DB": COMPANY,
                   "SAP_B1_USERNAME": USER, "SAP_B1_PASSWORD": PASSWORD,
                   "SAP_B1_PATH": "/b1s/v1", "SAP_B1_LABEL": "mock-b1",
                   "SAP_B1_WRITE_ENABLED": "1"}
        mcp_proc = start([sys.executable, "mcp_server_b1.py", "--http", "--port",
                          str(MCP_PORT)], mcp_env, "/tmp/sap-b1-verify-mcp.log")
        wait_port("127.0.0.1", MCP_PORT)
        mcp = McpHttp(MCP_URL)
        initialised = mcp.initialize()
        assert initialised.get("result", {}).get("serverInfo", {}).get("name") == "sap-b1", \
            initialised

        expected = {"b1_health", "b1_list_business_partners", "b1_get_business_partner",
                    "b1_list_items", "b1_list_orders", "b1_list_invoices",
                    "b1_list_purchase_orders", "b1_count", "b1_query",
                    "b1_create_business_partner", "b1_update_business_partner"}

        def mcp_tools() -> str:
            got = set(mcp.tools())
            assert got == expected, sorted(expected ^ got)
            return f"{len(got)} tools exposed to the agent"
        check("MCP: tools/list exposes the B1 toolset", mcp_tools)

        def mcp_reads() -> str:
            allbp = mcp.call_tool("b1_list_business_partners", {"top": 3, "count": True})
            assert allbp["ok"] and allbp["row_count"] == 3, allbp
            sa = mcp.call_tool("b1_list_business_partners",
                               {"country": "SA", "top": 50, "count": True})
            assert sa["row_count"] == sa["count"], (sa["row_count"], sa.get("count"))
            assert all(r["Country"] == "SA" for r in sa["items"]), sa["items"][:2]
            assert sa["count"] >= 14, sa["count"]
            one = mcp.call_tool("b1_get_business_partner", {"card_code": "C0005"})
            assert one["item"]["CardCode"] == "C0005", one
            inv = mcp.call_tool("b1_list_invoices", {"card_code": "C0008"})
            assert inv["row_count"] == 1, inv
            return (f"list(count={allbp['count']}) / country filter={sa['row_count']} / "
                    f"single read + invoices OK")
        check("MCP: tools/call reads return real data through the server", mcp_reads)

        def mcp_write() -> str:
            made = mcp.call_tool("b1_create_business_partner",
                                 {"card_code": "C9200", "card_name": "Agent Created Co",
                                  "card_type": "cCustomer", "country": "SA",
                                  "dry_run": False})
            assert made["executed"] is True, made
            renamed = mcp.call_tool("b1_update_business_partner",
                                    {"card_code": "C9200", "dry_run": False,
                                     "card_name": "Agent Created Co (v2)"})
            assert renamed["executed"] is True and renamed.get("read_back_ok") is True, renamed
            dry = mcp.call_tool("b1_create_business_partner",
                                {"card_code": "C9201", "card_name": "Never Written"})
            assert dry["executed"] is False, dry
            return "create + update executed with read-back_ok, dry-run still safe"
        check("MCP: tools/call write path executes and verifies via read-back", mcp_write)

        def mcp_health_tool() -> str:
            health_tool = mcp.call_tool("b1_health", {})
            assert health_tool["ok"] and health_tool["probe"]["row_count"] >= 1, health_tool
            assert health_tool["system"]["company_db"] == COMPANY, health_tool["system"]
            assert "password" not in json.dumps(health_tool["system"]).lower() or \
                health_tool["system"].get("password_set") is True, health_tool["system"]
            return f"company={health_tool['system']['company_db']}, password never returned"
        check("MCP: health tool reports the company without leaking the password",
              mcp_health_tool)

    finally:
        for proc in (mcp_proc, mock):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("== SAP B1 connector verification ==")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  --  {detail}" if detail else ""))
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    if passed != len(RESULTS):
        print("\nfailed checks:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
