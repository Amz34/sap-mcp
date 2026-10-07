"""MCP server exposing the SAP OData connector to any MCP-capable agent.

Transports
----------
  stdio (default)   : python mcp_server.py --stdio      -> Claude Code / Codex / OpenCode
  streamable HTTP   : python mcp_server.py --http       -> Hermes (mcp_servers: <url>) etc.

Which SAP system it talks to comes from the environment (SAP_BASE_URL, SAP_APIKEY or
SAP_USERNAME/SAP_PASSWORD, SAP_CLIENT ...) or ~/.config/sap-connector/config.json.
Governance: reads are open, writes are dry-run unless the operator set SAP_WRITE_ENABLED=1.
"""

import argparse
import asyncio
import inspect
import sys
from typing import Any, Optional

from mcp.server import MCPServer

from sap_odata import tools as sap

INSTRUCTIONS = """SAP OData connector (read-first, whitelisted services).

Always call sap_health first: it tells you which system is configured, whether it is
reachable, and whether writes are allowed. Read tools cover business partners,
suppliers, sales orders, purchase orders and products; sap_odata_query is a read-only
escape hatch. sap_create_business_partner is dry-run by default - a real POST needs the
operator to set SAP_WRITE_ENABLED=1, and any real write must be reported back with the
SAP document number for audit.
"""

mcp = MCPServer(
    name="sap-odata",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

# ----------------------------------------------------------------- read tools


@mcp.tool()
def sap_health() -> dict:
    """Check the SAP connector: configured system, reachability, and whether writes are enabled."""
    return sap.sap_health()


@mcp.tool()
def sap_list_business_partners(top: int = 20, country: Optional[str] = None,
                               category: Optional[str] = None, search: Optional[str] = None) -> dict:
    """List SAP business partners (customers/suppliers). country='SA', category='1'=org,
    search = substring of the partner name."""
    return sap.sap_list_business_partners(top=top, country=country, category=category, search=search)


@mcp.tool()
def sap_get_business_partner(business_partner: str) -> dict:
    """Fetch one business partner by SAP ID, e.g. 'BP0001'."""
    return sap.sap_get_business_partner(business_partner)


@mcp.tool()
def sap_list_sales_orders(top: int = 20, sold_to_party: Optional[str] = None,
                          customer_reference: Optional[str] = None) -> dict:
    """List sales orders (SD); filter by customer ID or by the customer's own reference."""
    return sap.sap_list_sales_orders(top=top, sold_to_party=sold_to_party,
                                     customer_reference=customer_reference)


@mcp.tool()
def sap_list_purchase_orders(top: int = 20, supplier: Optional[str] = None,
                             company_code: Optional[str] = None) -> dict:
    """List purchase orders (MM); filter by supplier ID or company code."""
    return sap.sap_list_purchase_orders(top=top, supplier=supplier, company_code=company_code)


@mcp.tool()
def sap_list_products(top: int = 20, product_type: Optional[str] = None,
                      product_group: Optional[str] = None) -> dict:
    """List materials/products. product_type: 'FERT' finished, 'ROH' raw, 'HAWA' trading."""
    return sap.sap_list_products(top=top, product_type=product_type, product_group=product_group)


@mcp.tool()
def sap_list_suppliers(top: int = 20, country: Optional[str] = None) -> dict:
    """List supplier master data, optionally filtered by country code."""
    return sap.sap_list_suppliers(top=top, country=country)


@mcp.tool()
def sap_odata_query(service: str, entity: Optional[str] = None, filter: Optional[str] = None,
                    select: Optional[str] = None, orderby: Optional[str] = None, top: int = 20) -> dict:
    """Read-only OData escape hatch. service: business_partner | sales_order |
    purchase_order | product | supplier. filter = raw OData $filter, e.g. "Country eq 'SA'"."""
    return sap.sap_odata_query(service=service, entity=entity, filter=filter, select=select,
                               orderby=orderby, top=top)


# ----------------------------------------------------------------- write tool


@mcp.tool()
def sap_create_business_partner(business_partner: str, name: str, country: str = "SA",
                                category: str = "1", dry_run: bool = True) -> dict:
    """Create a business partner. dry_run=True (default) returns the exact payload without
    POSTing; a real write additionally needs SAP_WRITE_ENABLED=1 on this process."""
    return sap.sap_create_business_partner(business_partner=business_partner, name=name,
                                           country=country, category=category, dry_run=dry_run)


# ----------------------------------------------------------------- entry point


def _run_stdio(app: Any) -> None:
    sig = inspect.signature(app.run)
    if "transport" in sig.parameters:
        app.run(transport="stdio")
    else:
        app.run()


def _run_http(app: Any, host: str, port: int, path: str) -> None:
    sig = inspect.signature(app.run_streamable_http_async)
    wanted = {"host": host, "port": port, "streamable_http_path": path, "path": path,
              "json_response": True, "stateless_http": True}
    kwargs = {k: v for k, v in wanted.items() if k in sig.parameters}
    print(f"[sap-mcp] streamable HTTP on http://{host}:{port}{path} (kwargs={sorted(kwargs)})",
          file=sys.stderr, flush=True)
    asyncio.run(app.run_streamable_http_async(**kwargs))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SAP OData MCP server")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="stdio transport (default)")
    mode.add_argument("--http", action="store_true", help="streamable HTTP transport")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args(argv)

    if args.http:
        _run_http(mcp, args.host, args.port, args.path)
    else:
        _run_stdio(mcp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
