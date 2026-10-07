#!/usr/bin/env python3
"""End-to-end verification of the SAP OData connector stack (no SAP account needed).

    python3 tests/verify_stack.py

Boots a mock SAP gateway (uvicorn) + the MCP server (streamable HTTP), then proves:

  client level : api-key gate 401, $filter, $select, $top/$skip paging, single-key read,
                 404 handling, CSRF handshake, governance write-gate, real write + read-back,
                 duplicate-key 409, OData V4 response shape
  agent level  : MCP tools/list + tools/call over raw JSON-RPC (raw HTTP = no client lib lies)

Exit 0 = all checks passed. Server logs: /tmp/sap-verify-mock.log, /tmp/sap-verify-mcp.log
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import socket  # noqa: E402

import httpx  # noqa: E402

from sap_odata.client import SapODataClient, SapODataError, SapWriteBlocked  # noqa: E402
from sap_odata.config import SANDBOX_ODATA_PATH, SapConfig  # noqa: E402


def _free_port(preferred: int) -> int:
    """Return `preferred` if free, else an ephemeral port.

    Guards the classic trap: if the default port is already taken by another
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


MOCK_KEY = "verify-key"
MOCK_PORT = _free_port(int(os.environ.get("VERIFY_MOCK_PORT", "8792")))
MCP_PORT = _free_port(int(os.environ.get("VERIFY_MCP_PORT", "8799")))  # 8791 is the systemd sap-mcp.service
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"
V4_PATH = "/sap/opu/odata4/sap"

EXPECTED_TOOLS = [
    "sap_health", "sap_list_business_partners", "sap_get_business_partner",
    "sap_list_sales_orders", "sap_list_purchase_orders", "sap_list_products",
    "sap_list_suppliers", "sap_odata_query", "sap_create_business_partner",
]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn: Callable[[], Any]) -> None:
    try:
        detail = fn()
        RESULTS.append((name, True, str(detail or "")))
    except AssertionError as exc:
        RESULTS.append((name, False, f"ASSERT: {exc}"))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------- helpers


def v2_config(**over: Any) -> SapConfig:
    base = dict(label="mock-v2", base_url=MOCK_BASE, odata_path=SANDBOX_ODATA_PATH, api_key=MOCK_KEY)
    base.update(over)
    return SapConfig(**base)


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=3.0).status_code < 500:
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.6)
    raise RuntimeError(f"service never came up: {url}")


def wait_port(host: str, port: int, timeout: float = 30.0) -> None:
    """TCP-level readiness check (a GET on an MCP streamable endpoint opens an SSE stream
    that never closes, so HTTP polling would hang)."""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return
        except OSError:
            time.sleep(0.4)
    raise RuntimeError(f"port never opened: {host}:{port}")


def start(cmd: list[str], env: dict[str, str], log_file: str) -> subprocess.Popen:
    handle = open(log_file, "w")  # noqa: SIM115
    return subprocess.Popen(cmd, cwd=str(ROOT), env={**os.environ, **env},
                            stdout=handle, stderr=subprocess.STDOUT)


class McpHttp:
    """Tiny raw JSON-RPC client for the streamable-HTTP transport."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.session: str | None = None
        self.version = "2025-06-18"
        self.http = httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "mcp-protocol-version": self.version}
        if self.session:
            h["mcp-session-id"] = self.session
        return h

    def _post(self, payload: dict, notify: bool = False) -> dict:
        resp = self.http.post(self.url, json=payload, headers=self._headers())
        self.session = resp.headers.get("mcp-session-id") or self.session
        if notify:
            return {}
        if resp.status_code >= 400:
            raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise AssertionError(f"no SSE data event in: {resp.text[:200]}")
        return resp.json()

    def initialize(self) -> dict:
        res = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": self.version, "capabilities": {},
                                     "clientInfo": {"name": "verify-stack", "version": "0.1"}}})
        self.version = res.get("result", {}).get("protocolVersion", self.version)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
        return res

    def call(self, method: str, params: dict | None = None, rid: int = 2) -> dict:
        res = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if "error" in res:
            raise AssertionError(f"JSON-RPC error: {res['error']}")
        return res.get("result", {})

    def tools(self) -> list[str]:
        return [t["name"] for t in self.call("tools/list", {}).get("tools", [])]

    def call_tool(self, name: str, args: dict, rid: int = 3) -> dict:
        result = self.call("tools/call", {"name": name, "arguments": args}, rid)
        for item in result.get("content", []):
            if item.get("type") == "text":
                return json.loads(item["text"])
        raise AssertionError(f"tool {name} returned no text content: {result}")


# ---------------------------------------------------------------------- checks


def client_checks() -> None:
    def api_key_gate() -> str:
        client = SapODataClient(SapConfig(label="no-key", base_url=MOCK_BASE, odata_path=SANDBOX_ODATA_PATH))
        try:
            client.query("business_partner", top=1)
            raise AssertionError("expected SapODataError for a keyless call")
        except SapODataError as exc:
            assert exc.status == 401, f"status={exc.status}"
            blob = f"{exc.code} {exc.message} {json.dumps(exc.details or {})}"
            assert "API Key" in blob, blob
            assert "FailedToResolveAPIKey" in blob, blob
            return "401 -> " + str(exc.code)[:44]
        finally:
            client.close()

    def filter_and_select() -> str:
        client = SapODataClient(v2_config())
        try:
            page = client.query("business_partner", top=50, filter="Country eq 'SA'",
                                select="BusinessPartner,Country,BusinessPartnerName")
            assert page["items"], "no rows returned"
            assert all(r["Country"] == "SA" for r in page["items"]), "filter leaked"
            assert all(set(r) <= {"BusinessPartner", "Country", "BusinessPartnerName"} for r in page["items"])
            return f"{len(page['items'])} SA rows, $select honoured"
        finally:
            client.close()

    def paging() -> str:
        client = SapODataClient(v2_config())
        try:
            page1 = client.query("business_partner", top=5, count=True)
            page2 = client.query("business_partner", top=5, skip=5)
            keys1 = {r["BusinessPartner"] for r in page1["items"]}
            keys2 = {r["BusinessPartner"] for r in page2["items"]}
            assert len(keys1) == 5 and len(keys2) == 5, (len(keys1), len(keys2))
            assert not (keys1 & keys2), f"pages overlap: {keys1 & keys2}"
            assert page1["count"] == 25, f"inlinecount={page1['count']}"
            assert page1.get("next"), "no next-page link surfaced"
            return "5+5 disjoint of 25, next-link present"
        finally:
            client.close()

    def single_read_and_404() -> str:
        client = SapODataClient(v2_config())
        try:
            bp = client.get_entity("business_partner", "BP0001")
            assert bp["item"]["BusinessPartner"] == "BP0001", bp
            try:
                client.get_entity("business_partner", "BP9999")
                raise AssertionError("expected 404 for unknown key")
            except SapODataError as exc:
                assert exc.status == 404, exc.status
            name = bp["item"].get("BusinessPartnerName") or bp["item"].get("BusinessPartnerFullName") or "-"
            return f"{bp['item']['BusinessPartner']} ({name}) / 404 surfaced"
        finally:
            client.close()

    def write_gate() -> str:
        client = SapODataClient(v2_config(write_enabled=False))
        try:
            try:
                client.create_entity("business_partner",
                                     {"BusinessPartner": "BP9001", "BusinessPartnerName": "Gate Test"},
                                     dry_run=False)
                raise AssertionError("write left the box with the gate closed")
            except SapWriteBlocked as exc:
                assert "SAP_WRITE_ENABLED" in str(exc), str(exc)
            try:
                client.get_entity("business_partner", "BP9001")
                raise AssertionError("a non-dry-run write was blocked but the object exists")
            except SapODataError as exc:
                assert exc.status == 404, exc.status
            return "SapWriteBlocked raised, no POST sent, object absent"
        finally:
            client.close()

    def real_write_and_readback() -> str:
        client = SapODataClient(v2_config(write_enabled=True))
        try:
            created = client.create_entity("business_partner", {
                "BusinessPartner": "BP9001", "BusinessPartnerCategory": "1",
                "BusinessPartnerName": "Verify Industries", "BusinessPartnerFullName": "Verify Industries",
                "Country": "SA"}, dry_run=False)
            assert created.get("executed") is True, created
            back = client.get_entity("business_partner", "BP9001")
            assert back["item"]["BusinessPartnerName"] == "Verify Industries", back
            try:
                client.create_entity("business_partner", {"BusinessPartner": "BP9001",
                                                          "BusinessPartnerName": "Dup"}, dry_run=False)
                raise AssertionError("duplicate create was not rejected")
            except SapODataError as exc:
                assert exc.status == 409, exc.status
            return "201 -> read-back OK -> duplicate 409"
        finally:
            client.close()

    def v4_shape() -> str:
        client = SapODataClient(v2_config(odata_path=V4_PATH, odata_version="v4", label="mock-v4"))
        try:
            page = client.query("business_partner", top=3, select="BusinessPartner,Country")
            assert len(page["items"]) == 3, page["items"]
            assert page["shape"] == "v4", page["shape"]
            assert isinstance(page["count"], int) and page["count"] >= 25, page["count"]
            return f"v4 value[] + @odata.count={page['count']}"
        finally:
            client.close()

    def tools_layer() -> str:
        os.environ.update({"SAP_BASE_URL": MOCK_BASE, "SAP_ODATA_PATH": SANDBOX_ODATA_PATH,
                           "SAP_APIKEY": MOCK_KEY, "SAP_LABEL": "mock-v2", "SAP_WRITE_ENABLED": "0"})
        from sap_odata import tools as sap
        health = sap.sap_health()
        assert health["ok"] and health["reachable"], health
        pos = sap.sap_list_purchase_orders(top=3)
        assert pos["ok"] and pos["count"] == 3, pos
        bad = sap.sap_odata_query(service="not_a_service")
        assert bad["ok"] is False and bad["error"]["error"] == "UNKNOWN_SERVICE", bad
        dry = sap.sap_create_business_partner(business_partner="BP9100", name="Dry Run Co")
        assert dry["ok"] and dry["dry_run"] is True, dry
        blocked = sap.sap_create_business_partner(business_partner="BP9100", name="Dry Run Co", dry_run=False)
        assert blocked["ok"] is False and blocked["error"]["error"] == "SAP_WRITE_BLOCKED", blocked
        return "health/list/unknown-service/dry-run/write-blocked all behaved"

    check("client: api-key gate -> 401 SAP fault", api_key_gate)
    check("client: $filter + $select", filter_and_select)
    check("client: $top/$skip paging + inlinecount", paging)
    check("client: single-key read + 404 path", single_read_and_404)
    check("governance: write gate blocks POST", write_gate)
    check("client: CSRF write -> read-back -> 409 dup", real_write_and_readback)
    check("client: OData V4 response shape", v4_shape)
    check("tools: dict-returning layer + failure modes", tools_layer)


def mcp_checks() -> None:
    mcp = McpHttp(MCP_URL)
    mcp.initialize()

    def tool_list() -> str:
        names = mcp.tools()
        missing = [n for n in EXPECTED_TOOLS if n not in names]
        assert not missing, f"missing tools: {missing}"
        return f"{len(names)} tools exposed"

    def call_read_tool() -> str:
        out = mcp.call_tool("sap_list_business_partners", {"top": 3, "country": "SA"}, rid=11)
        assert out["ok"] is True, out
        assert out["count"] == 3, out["count"]
        return f"count={out['count']}, system={out['system']['label']}"

    def call_health() -> str:
        out = mcp.call_tool("sap_health", {}, rid=12)
        assert out["ok"] is True and out["reachable"] is True, out
        return f"reachable, writes={out['system']['write_enabled']}"

    def call_write_tool() -> str:
        dry = mcp.call_tool("sap_create_business_partner",
                            {"business_partner": "BP9200", "name": "Agent Dry Co"}, rid=13)
        assert dry["ok"] is True and dry["dry_run"] is True, dry
        real = mcp.call_tool("sap_create_business_partner",
                             {"business_partner": "BP9200", "name": "Agent Real Co", "dry_run": False}, rid=14)
        assert real["ok"] is True and real.get("dry_run") is False, real
        client = SapODataClient(v2_config())
        try:
            back = client.get_entity("business_partner", "BP9200")
            assert back["item"]["BusinessPartnerName"] == "Agent Real Co", back
        finally:
            client.close()
        return "dry-run OK; real create via MCP verified by independent read-back"

    check("mcp: tools/list exposes the full surface", tool_list)
    check("mcp: tools/call read tool", call_read_tool)
    check("mcp: tools/call sap_health", call_health)
    check("mcp: tools/call write tool + read-back", call_write_tool)


def main() -> int:
    mock_env = {"MOCK_SAP_APIKEY": MOCK_KEY}
    mcp_env = {"SAP_BASE_URL": MOCK_BASE, "SAP_ODATA_PATH": SANDBOX_ODATA_PATH, "SAP_APIKEY": MOCK_KEY,
               "SAP_LABEL": "mock-v2", "SAP_WRITE_ENABLED": "1"}
    mock = start([sys.executable, "-m", "uvicorn", "mock_sap.server:app", "--host", "127.0.0.1",
                  "--port", str(MOCK_PORT), "--log-level", "warning"], mock_env, "/tmp/sap-verify-mock.log")
    mcp_proc = None
    try:
        wait_http(f"{MOCK_BASE}/healthz")
        info = httpx.get(f"{MOCK_BASE}/healthz", timeout=5).json()
        print(f"[start] mock SAP gateway up: {info['rows']}")
        mcp_proc = start([sys.executable, "mcp_server.py", "--http", "--port", str(MCP_PORT)],
                         mcp_env, "/tmp/sap-verify-mcp.log")
        wait_port("127.0.0.1", MCP_PORT)
        try:
            McpHttp(MCP_URL).initialize()
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
        print("[start] MCP server up\n")

        client_checks()
        mcp_checks()
    finally:
        for proc in (mcp_proc, mock):
            if proc is not None:
                proc.terminate()
        time.sleep(0.3)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    width = max(len(n) for n, _, _ in RESULTS)
    print("CHECK".ljust(width) + "  RESULT  DETAIL")
    for name, ok, detail in RESULTS:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL':<6}  {detail}")
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    if passed != len(RESULTS):
        print("see /tmp/sap-verify-mock.log and /tmp/sap-verify-mcp.log")
        return 1
    print("SAP connector stack verified end-to-end (mock sandbox, no SAP account used).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
