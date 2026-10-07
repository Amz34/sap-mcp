# SAP OData connector + MCP tool server

Read-first SAP connector that turns SAP OData APIs into agent tools. Runs on this
ARM64 VM, needs **no SAP licence, no BTP subscription, no ABAP developer, and no
SAP NetWeaver RFC SDK** (which does not exist for aarch64 - see *Known limits*).

## What is proven right now

`python3 tests/verify_stack.py` (use `.venv/bin/python` if `mcp` is not importable in the
default interpreter) -> **12/12 checks passed** against a local mock SAP gateway, covering both
the connector and the agent (MCP) layer. Latest run saved at `tests/last-verify-output.txt`:

| check | detail |
|---|---|
| api-key gate -> 401 SAP fault | `401 -> Failed to resolve API Key variable request.header.apikey` |
| `$filter` + `$select` | 4 SA rows, only requested fields returned |
| `$top`/`$skip` paging + inlinecount | 5+5 disjoint rows of 25, `__next` link present |
| single-key read + 404 path | `BP0001 (Al Rajhi Industrial)` / 404 surfaced as SAP error |
| governance: write gate blocks POST | `SapWriteBlocked`, no POST sent, object absent |
| CSRF write -> read-back -> 409 dup | 201 -> independent read-back OK -> duplicate rejected |
| OData V4 response shape | `value[]` + `@odata.count` parsed |
| tools layer failure modes | health / unknown service / dry-run / write-blocked |
| MCP `tools/list` | 9 tools exposed |
| MCP `tools/call` read | `count=3`, `system=mock-v2` |
| MCP `tools/call` health | reachable, writes reported |
| MCP `tools/call` write + read-back | dry-run OK; real create verified by independent read |

Real-endpoint proof (no SAP account needed): a call to the live SAP Business
Accelerator Hub gateway (`https://sandbox.api.sap.com`) is answered by SAP itself
with `401 Failed to resolve API Key variable request.header.apikey`
(`errorcode: steps.oauth.v2.FailedToResolveAPIKey`) - our client reaches the real
SAP gateway and maps SAP's own fault structure correctly.

## Layout

```
sap_odata/config.py   env/profile config, per-client profiles, write gate
sap_odata/client.py   OData V2+V4 client: paging, $filter/$select, CSRF, retries, SAP errors
sap_odata/tools.py    agent-facing tools (always return dicts, never raise)
mcp_server.py         MCP server: stdio + streamable HTTP
mock_sap/server.py    local mock of the SAP sandbox gateway (dev + CI only)
tests/verify_stack.py end-to-end verification (boots mock + MCP, asserts 12 checks)
```

## The 9 tools an agent gets

`sap_health`, `sap_list_business_partners`, `sap_get_business_partner`,
`sap_list_sales_orders`, `sap_list_purchase_orders`, `sap_list_products`,
`sap_list_suppliers`, `sap_odata_query` (read-only escape hatch),
`sap_create_business_partner` (dry-run by default).

## Quickstart - 1) local mock (no SAP account)

```bash
cd ~/sap-connector
.venv/bin/python -m uvicorn mock_sap.server:app --host 127.0.0.1 --port 8792 &   # mock gateway
MOCK_SAP_APIKEY=verify-key SAP_BASE_URL=http://127.0.0.1:8792 \
SAP_APIKEY=verify-key SAP_LABEL=mock .venv/bin/python mcp_server.py --http --port 8799   # MCP on :8799
```

## Quickstart - 2) real SAP sandbox (free, 5 minutes)

1. Register a free SAP Universal ID at <https://api.sap.com> -> *Settings ->
   API Key* (this is a user step: it needs SAP's own signup/email verification).
2. Put it in `~/.config/sap-connector/env`:

```
SAP_LABEL=sap-sandbox
SAP_BASE_URL=https://sandbox.api.sap.com
SAP_ODATA_PATH=/s4hanacloud/sap/opu/odata/sap
SAP_APIKEY=<your free key>
SAP_WRITE_ENABLED=0
```

3. `systemctl --user restart sap-mcp.service && hermes mcp test sap-odata`

## Quickstart - 3) a client's real system

```bash
# S/4HANA or ECC with SAP Gateway (basic auth + client)
SAP_BASE_URL=https://sap.example.com:44300
SAP_ODATA_PATH=/sap/opu/odata/sap
SAP_AUTH_TYPE=basic SAP_USERNAME=AGENT_COMM SAP_PASSWORD=... SAP_CLIENT=100

# S/4HANA Cloud public edition (OAuth2 client credentials, communication arrangement)
SAP_AUTH_TYPE=oauth2 SAP_OAUTH_TOKEN_URL=https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token
SAP_OAUTH_CLIENT_ID=... SAP_OAUTH_CLIENT_SECRET=...
```

Writes stay impossible until the operator sets `SAP_WRITE_ENABLED=1`; every tool
call works in dry-run first and returns the exact payload that would be POSTed.

## Multi-client profiles

`~/.config/sap-connector/config.json` (chmod 600):

```json
{"active": "client_a",
 "profiles": {
   "client_a": {"label": "Client A S/4", "base_url": "https://...", "auth_type": "basic",
                "username": "AGENT_COMM", "password": "...", "sap_client": "100",
                "write_enabled": false},
   "sandbox":  {"label": "SAP sandbox", "base_url": "https://sandbox.api.sap.com",
                "api_key": "..."}}}
```

Environment variables always override the file, so each client can get its own
systemd unit + env file + vault space instead of sharing one.

## MCP clients

* **Hermes**: `hermes mcp add sap-odata --url http://127.0.0.1:8791/mcp`
* **Claude Code / Codex / OpenCode** (stdio): command
  `/path/to/venv/bin/python`, args `/path/to/sap-mcp/mcp_server.py --stdio`
  + the `SAP_*` env vars from `.env.example`.
* **n8n**: HTTP Request node against the same OData URLs, or an MCP client node
  pointed at `http://127.0.0.1:8791/mcp`.

## Known limits (deliberate, documented)

* **OData only.** RFC/BAPI/JCo/PyRFC are impossible on ARM Linux - the SAP
  NetWeaver RFC SDK ships for `linuxx86_64`/`ppc64le` only, so legacy RFC needs a
  separate x86_64 node.
* SAP Business One Service Layer is **not** part of this server - it ships as its
  own entrypoint and mock in this repo, see [`B1-CONNECTOR.md`](B1-CONNECTOR.md).
* **No IDoc ingest yet** - IDoc over SOAP/SFTP is the next connector in the queue.
* **Read tools are capped** (`$top`, `page_size`, `max_pages`) so an agent cannot
  accidentally pull 500k rows into a context window. Use `query_all` deliberately.
* **`$select` field names vary by SAP release** - the client retries without
  `$select` if a gateway rejects a field list.
* Every write must be logged with the returned SAP document number (audit trail);
  this is a process rule, not a code rule.
