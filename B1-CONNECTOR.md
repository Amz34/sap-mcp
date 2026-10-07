# SAP Business One Service Layer connector

Second connector in this project, alongside the OData one in `ODATA-CONNECTOR.md`. Same
principles: read-first, dry-run writes, `SAP_B1_WRITE_ENABLED` governance gate, and a
local mock so the whole stack can be proven without a customer system.

## What it speaks

| Item | Value |
|---|---|
| Endpoint | `https://<server>:50000/b1s/v1` (`SAP_B1_PATH` to override) |
| Auth | `POST /Login` (CompanyDB + UserName + Password) -> `B1SESSION` cookie; `POST /Logout` |
| Session expiry | SAP answers `401` (code `301`); the client re-logs in once and retries the call |
| Reads | `GET /<Collection>?$filter=&$select=&$orderby=&$top=&$skip=`, `GET /<Collection>/$count` |
| Single row | `GET /<Collection>('<key>')` |
| Writes | `POST /<Collection>` (create), `PATCH /<Collection>('<key>')` (update, returns `204`) |
| Errors | `{"error": {"code": ..., "message": {"lang": ..., "value": ...}}}` -> surfaced as `B1Error(code, status)` |

Collections mapped: `BusinessPartners` (CardCode), `Items` (ItemCode), `Orders`,
`Invoices`, `PurchaseOrders` (DocEntry).

## Tools exposed to the agent (11)

`b1_health`, `b1_list_business_partners`, `b1_get_business_partner`, `b1_list_items`,
`b1_list_orders`, `b1_list_invoices`, `b1_list_purchase_orders`, `b1_count`,
`b1_query`, `b1_create_business_partner`, `b1_update_business_partner`.

Writes default to `dry_run=True`, and any field outside the entity whitelist is
refused before a request leaves the box.

## Run it

```bash
# 1. local mock Service Layer + full verification (no SAP needed)
cd ~/sap-connector
.venv/bin/python tests/verify_b1.py        # 16/16 checks

# 2. against a real customer company
cp ~/.config/sap-connector/env-b1.example ~/.config/sap-connector/env-b1   # then fill it
systemctl --user restart sap-b1-mcp.service
hermes mcp test sap-b1

# 3. stdio mode for a coding agent
SAP_B1_BASE_URL=https://b1host:50000 SAP_B1_COMPANY_DB=SBODEMO \
SAP_B1_USERNAME=manager SAP_B1_PASSWORD=*** .venv/bin/python mcp_server_b1.py --stdio
```

Service: `sap-b1-mcp.service` (127.0.0.1:8793, env file `~/.config/sap-connector/env-b1`,
log `~/logs/sap-b1-mcp.log`). MCP name in Hermes: `sap-b1`.

## Verified behaviour (tests/verify_b1.py, all 16 pass)

Mock Service Layer up with 5 collections; wrong password refused with SAP's own error
envelope (`-1101`); unconfigured connector fails closed and names the missing env vars;
cookie auth plus one automatic re-login after the session cookie is dropped; filtered
`$filter`/`$select`/`contains()` reads and `$count` endpoint (5 suppliers of 17 business
partners); `$top`/`$skip` paging without overlap; single-key read and the 404 envelope;
write gate blocks POST and PATCH with nothing sent; dry-run sends nothing and the
required-field guard fires; real create (201) with independent read-back and duplicate
rejection (`-2028`); real PATCH (204) with read-back, primary-key change and unknown
fields refused; tool layer contract; MCP `tools/list` (11 tools) plus `tools/call` for
reads and an executed write with read-back; health tool that never returns the password.

## Notes for a real customer

* Business One usually needs a dedicated B1 user for this (not a shared human login) and
  the Service Layer must be enabled plus your egress IP allowed through the firewall.
* `$count` with a `$filter` is not accepted by every Service Layer build; the client
  degrades gracefully and reports `count_scope: "unavailable"` instead of failing.
* `PATCH` returns `204 No Content`, so every write is confirmed by a fresh read-back.
