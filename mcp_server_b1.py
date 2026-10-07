"""MCP server exposing the SAP Business One Service Layer connector as agent tools.

Transports
----------
  stdio (default)   : python mcp_server_b1.py --stdio      -> Claude Code / Codex / OpenCode
  streamable HTTP   : python mcp_server_b1.py --http       -> Hermes and other HTTP MCP clients

Which company you talk to comes from the environment (SAP_B1_BASE_URL, SAP_B1_COMPANY_DB,
SAP_B1_USERNAME, SAP_B1_PASSWORD, SAP_B1_PROFILE) or ~/.config/sap-b1/config.json.
Reads are safe. Writes are dry-run unless SAP_B1_WRITE_ENABLED=1 and the caller passes
dry_run=False; every executed write reports a read-back for the audit trail.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from typing import Any, Optional

from mcp.server import MCPServer

from sap_b1 import tools as b1

INSTRUCTIONS = """SAP Business One (Service Layer) connector - read-first.

Call b1_health first: it reports the configured company, login state and a probe read.
Reads: b1_list_business_partners, b1_get_business_partner, b1_list_items, b1_list_orders,
b1_list_invoices, b1_list_purchase_orders, b1_count, b1_query.
Writes: b1_create_business_partner, b1_update_business_partner - dry-run by default; a real
write needs the operator to have enabled SAP_B1_WRITE_ENABLED and the caller to pass
dry_run=False. Never invent a CardCode: read it first or ask the user.
"""

mcp = MCPServer(name="sap-b1", version="0.1.0", instructions=INSTRUCTIONS)


@mcp.tool()
def b1_health() -> dict[str, Any]:
    """Check configuration, Service Layer login and a one-row read probe on the B1 company."""
    return b1.b1_health()


@mcp.tool()
def b1_list_business_partners(top: int = 20, card_type: Optional[str] = None,
                              country: Optional[str] = None, search: Optional[str] = None,
                              count: bool = False) -> dict[str, Any]:
    """List business partners. card_type: cCustomer, cSupplier or cLead; search matches CardName."""
    return b1.b1_list_business_partners(top=top, card_type=card_type, country=country,
                                        search=search, count=count)


@mcp.tool()
def b1_get_business_partner(card_code: str) -> dict[str, Any]:
    """Read one business partner by its CardCode (for example C0001)."""
    return b1.b1_get_business_partner(card_code)


@mcp.tool()
def b1_list_items(top: int = 20, search: Optional[str] = None,
                  count: bool = False) -> dict[str, Any]:
    """List items, optionally filtering on the item name."""
    return b1.b1_list_items(top=top, search=search, count=count)


@mcp.tool()
def b1_list_orders(top: int = 20, card_code: Optional[str] = None,
                   count: bool = False) -> dict[str, Any]:
    """List sales orders, newest first, optionally for one customer CardCode."""
    return b1.b1_list_orders(top=top, card_code=card_code, count=count)


@mcp.tool()
def b1_list_invoices(top: int = 20, card_code: Optional[str] = None,
                     count: bool = False) -> dict[str, Any]:
    """List A/R invoices, newest first, optionally for one customer CardCode."""
    return b1.b1_list_invoices(top=top, card_code=card_code, count=count)


@mcp.tool()
def b1_list_purchase_orders(top: int = 20, card_code: Optional[str] = None,
                            count: bool = False) -> dict[str, Any]:
    """List purchase orders, newest first, optionally for one vendor CardCode."""
    return b1.b1_list_purchase_orders(top=top, card_code=card_code, count=count)


@mcp.tool()
def b1_count(entity: str) -> dict[str, Any]:
    """Row count for one entity. entity: business_partner, item, order, invoice, purchase_order."""
    return b1.b1_count(entity)


@mcp.tool()
def b1_query(entity: str, filter: Optional[str] = None, select: Optional[str] = None,
             top: int = 20, skip: int = 0, orderby: Optional[str] = None,
             count: bool = False) -> dict[str, Any]:
    """Read-only advanced query (raw Service Layer OData syntax) for the entity names above."""
    return b1.b1_query(entity, filter=filter, select=select, top=top, skip=skip,
                       orderby=orderby, count=count)


@mcp.tool()
def b1_create_business_partner(card_code: str, card_name: str,
                               card_type: str = "cCustomer", dry_run: bool = True,
                               country: Optional[str] = None, currency: Optional[str] = None,
                               email: Optional[str] = None, phone: Optional[str] = None,
                               group_code: Optional[int] = None) -> dict[str, Any]:
    """Create a business partner. Dry-run by default; a real POST also needs the write gate open."""
    return b1.b1_create_business_partner(card_code=card_code, card_name=card_name,
                                         card_type=card_type, dry_run=dry_run,
                                         country=country, currency=currency, email=email,
                                         phone=phone, group_code=group_code)


@mcp.tool()
def b1_update_business_partner(card_code: str, dry_run: bool = True,
                               card_name: Optional[str] = None,
                               email: Optional[str] = None,
                               phone: Optional[str] = None,
                               city: Optional[str] = None,
                               country: Optional[str] = None,
                               notes: Optional[str] = None) -> dict[str, Any]:
    """Update a business partner (PATCH) - dry-run by default; a real PATCH needs the write gate."""
    return b1.b1_update_business_partner(card_code, dry_run=dry_run, CardName=card_name,
                                         EmailAddress=email, Phone1=phone, City=city,
                                         Country=country, Notes=notes)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SAP Business One MCP tool server")
    parser.add_argument("--stdio", action="store_true", help="serve over stdio (default)")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args(argv)

    if args.http:
        kwargs: dict[str, Any] = {}
        sig = inspect.signature(mcp.run_streamable_http_async)
        for key, value in (("host", args.host), ("port", args.port),
                           ("streamable_http_path", args.path), ("json_response", True),
                           ("stateless_http", True)):
            if key in sig.parameters:
                kwargs[key] = value
        print(f"[sap-b1] streamable HTTP on http://{args.host}:{args.port}{args.path}",
              file=sys.stderr, flush=True)
        asyncio.run(mcp.run_streamable_http_async(**kwargs))
        return 0

    mcp.run(transport="stdio") if "transport" in inspect.signature(mcp.run).parameters \
        else mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
