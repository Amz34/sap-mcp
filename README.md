# sap-mcp — SAP MCP servers (OData + Business One)

Two MCP servers that give an AI agent structured access to SAP data:

| server | target | tools | transport | doc |
|---|---|---|---|---|
| `mcp_server.py` | SAP OData (`/sap/opu/odata/...`, S/4 sandbox style) | 9 × `sap_*` | stdio or HTTP `/mcp` | [`ODATA-CONNECTOR.md`](ODATA-CONNECTOR.md) |
| `mcp_server_b1.py` | SAP Business One Service Layer (`/b1s/v1`) | 11 × `b1_*` | stdio or HTTP `/mcp` | [`B1-CONNECTOR.md`](B1-CONNECTOR.md) |

Both follow the same rules, because handing an agent a write path into an ERP needs more
than a "trust me":

* **read-first** — the read surface is complete before any write tool exists;
* **writes are gated** — every write tool is blocked unless `SAP_WRITE_ENABLED=1`
  (OData) / `SAP_B1_WRITE_ENABLED=1` (Business One) is set in the server's own env;
* **writes are proven, not asserted** — a create/update is followed by an independent
  read-back and the result is reported as `read-back_ok`;
* **`dry_run` first** — write tools accept `dry_run=True` and return the exact payload
  they would send;
* **credentials never travel through tool arguments** — they come from the env/config
  file, and `*_health` reports the target *without* ever echoing the password;
* **no SAP account needed to believe any of this** — a local mock of each API ships in
  the repo and the test suite runs the whole stack against it.

## Prove it in two commands

```bash
pip install -r requirements.txt
python3 tests/verify_stack.py    # OData server, mock SAP sandbox
python3 tests/verify_b1.py       # Business One server, mock B1 service layer
```

Verified output on this repo (aarch64 Linux, Python 3.12, no SAP system involved):

```
OData  -> 12/12 checks passed
  PASS  mcp: tools/list exposes the full surface   -- 9 tools exposed
  PASS  mcp: tools/call write tool + read-back     -- dry-run OK; real create via MCP
                                                      verified by independent read-back
B1     -> 16/16 checks passed
  PASS  MCP: tools/list exposes the B1 toolset     -- 11 tools exposed
  PASS  MCP: health tool reports the company without leaking the password
                                                   -- company=SBODEMO, password never returned
```

The tests bind free ports automatically, so they can run next to a live server instead of
silently talking to whatever already holds port 8791/8795.

## Run the servers

```bash
# OData over streamable HTTP (endpoint: http://127.0.0.1:8791/mcp)
python3 mcp_server.py --http --host 127.0.0.1 --port 8791

# Business One over streamable HTTP (endpoint: http://127.0.0.1:8793/mcp)
python3 mcp_server_b1.py --http --host 127.0.0.1 --port 8793

# or stdio, for clients that spawn a process (Claude Code, Codex, OpenCode, Cursor)
python3 mcp_server.py --stdio
```

### Docker

The image runs the same stdio server, so any MCP client — or a hosted builder such as
Glama — can spawn and introspect it:

```bash
docker build -t sap-mcp .
docker run -i --rm \
  -e SAP_CONFIG_FILE=/config/sap.toml \
  -v "$PWD/sap.toml:/config/sap.toml:ro" sap-mcp
```

Connection settings come from `SAP_CONFIG_FILE`; every write stays behind the
`SAP_WRITE_ENABLED` gate.

Client entries:

```jsonc
// HTTP client (Hermes, n8n, anything that speaks streamable HTTP)
{ "mcpServers": { "sap-odata": { "url": "http://127.0.0.1:8791/mcp" } } }

// stdio client - command + args + the SAP_* env vars from .env.example
{ "mcpServers": { "sap-b1": { "command": "/path/to/venv/bin/python",
                              "args": ["/path/to/sap-mcp/mcp_server_b1.py", "--stdio"],
                              "env": { "SAP_B1_COMPANY_DB": "SBODEMO" } } } }
```

## Tools

**OData (`sap_*`)** — `sap_health`, `sap_list_business_partners`,
`sap_get_business_partner`, `sap_list_products`, `sap_list_sales_orders`,
`sap_list_purchase_orders`, `sap_list_suppliers`, `sap_odata_query`,
`sap_create_business_partner`.

**Business One (`b1_*`)** — `b1_health`, `b1_count`, `b1_query`,
`b1_list_business_partners`, `b1_get_business_partner`, `b1_list_items`,
`b1_list_orders`, `b1_list_invoices`, `b1_list_purchase_orders`,
`b1_create_business_partner`, `b1_update_business_partner`.

`*_query` takes an OData query string and returns JSON-safe dicts (never raw HTTP
objects), so the caller can reason over the result directly.

## Governance

| behaviour | detail |
|---|---|
| default posture | read + query; every write tool refuses with `SAP_WRITE_BLOCKED` / `B1_WRITE_BLOCKED` |
| enabling writes | set `SAP_WRITE_ENABLED=1` / `SAP_B1_WRITE_ENABLED=1` in the server's env, then restart it |
| dry run | `dry_run=True` returns the payload without sending it |
| proof | after a real write the server re-reads the record and returns `read-back_ok` |
| secrets | read from env or `SAP_CONFIG_FILE` profiles; never accepted as tool arguments and never echoed by `*_health` |

## Layout

```
mcp_server.py        OData MCP server (9 tools)
mcp_server_b1.py     Business One MCP server (11 tools)
sap_odata/           OData client, config, tool layer
sap_b1/              B1 client, config, tool layer
mock_sap/            local OData mock (FastAPI)
mock_b1/             local B1 Service Layer mock (FastAPI)
tests/verify_stack.py   12 end-to-end checks against the OData mock
tests/verify_b1.py      16 end-to-end checks against the B1 mock
```

## Limits (deliberate, documented)

* **OData/Service-Layer transport only.** RFC/BAPI/JCo is not supported: SAP's
  NetWeaver RFC SDK ships for `linuxx86_64` and `ppc64le` only, so a pure-Python stack
  on ARM Linux cannot load it. Legacy RFC needs a separate x86_64 node.
* **The mocks cover the shapes the tests exercise**, not every SAP edge case — point
  `SAP_BASE_URL`/`SAP_B1_BASE_URL` at a real system (or the SAP sandbox with an API key)
  for the real thing.
* **`sap_*` writes are limited to what the sample services expose** (e.g. create a
  business partner); extend `sap_odata/tools.py` rather than relaxing the gate.

## License

MIT — see `LICENSE`.

---

Part of [my always-on agent stack](https://github.com/Amz34) · [Awesome Agent Infrastructure](https://github.com/Amz34/awesome-agent-infrastructure) (135 live-checked building blocks).
