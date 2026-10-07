"""Agent-facing SAP tools: thin, JSON-serialisable wrappers over SapODataClient.

Contract for every tool below:
  * returns a plain dict, NEVER raises to the MCP layer
  * {"ok": true,  "system": {...}, "count": n, "items": [...], "url": "..."}
  * {"ok": false, "error": {...}}  on any failure

Read tools are wide open. Write tools are dry-run by default and require the
process env SAP_WRITE_ENABLED=1 before any POST leaves the box (governance gate).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .client import SapODataClient, SapODataError, SapWriteBlocked
from .config import SERVICES, SapConfig, load_config

# ---------------------------------------------------------------- helpers


def _client(config: Optional[SapConfig] = None) -> SapODataClient:
    return SapODataClient(config or load_config())


def _q(value: str) -> str:
    """Quote + escape an OData string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _fail(exc: Exception) -> dict:
    if isinstance(exc, SapODataError):
        return {"ok": False, "error": exc.to_dict()}
    if isinstance(exc, SapWriteBlocked):
        return {
            "ok": False,
            "error": {
                "error": "SAP_WRITE_BLOCKED",
                "message": str(exc),
                "hint": "Set SAP_WRITE_ENABLED=1 in the connector environment "
                "after the client has approved the write path.",
            },
        }
    return {"ok": False, "error": {"error": exc.__class__.__name__, "message": str(exc)}}


def _system(client: SapODataClient) -> dict:
    cfg = client.config
    return {"label": cfg.label, "base_url": cfg.base_url, "odata_path": cfg.odata_path,
            "odata_version": cfg.odata_version, "auth_type": cfg.auth_type,
            "sap_client": cfg.sap_client, "write_enabled": cfg.write_enabled}


def _read(client: SapODataClient, service: str, *, top: int, filter_: Optional[str] = None,
          select: Optional[str] = None, orderby: Optional[str] = None, entity: Optional[str] = None) -> dict:
    """Query with a $select fallback: SAP field sets drift between releases."""
    started = time.time()
    try:
        page = client.query(service, entity=entity, top=top, filter=filter_, select=select, orderby=orderby)
    except SapODataError as exc:
        if select and exc.status == 400:
            page = client.query(service, entity=entity, top=top, filter=filter_, orderby=orderby)
        else:
            raise
    out = {
        "ok": True,
        "system": _system(client),
        "service": service,
        "entity": entity or SERVICES[service]["entity"],
        "count": len(page["items"]),
        "total_matching": page.get("count"),
        "items": page["items"],
        "url": page["url"],
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if page.get("next"):
        out["more_available"] = True
    return out


# ---------------------------------------------------------------- read tools


def sap_health() -> dict:
    """Check the SAP connector: which system is configured, is it reachable, are writes allowed?

    Call this FIRST in any SAP task so you know which system you are talking to.
    """
    try:
        client = _client()
    except Exception as exc:  # config problems must also be readable
        return _fail(exc)
    info = {"ok": True, "system": _system(client), "seconds_remaining_budget": None}
    try:
        page = client.query("business_partner", top=1, select=SERVICES["business_partner"]["fields"][:2])
        info["reachable"] = True
        info["probe"] = {"entity": "A_BusinessPartner", "rows": len(page["items"]), "url": page["url"]}
    except SapODataError as exc:
        info["ok"] = False
        info["reachable"] = False
        info["probe_error"] = exc.to_dict()
    return info


def sap_list_business_partners(top: int = 20, country: Optional[str] = None,
                               category: Optional[str] = None, search: Optional[str] = None) -> dict:
    """List business partners (customers/suppliers). country='SA', category='1'=Organisation,
    search matches the partner name (substringof)."""
    try:
        client = _client()
    except Exception as exc:
        return _fail(exc)
    clauses = []
    if country:
        clauses.append(f"Country eq {_q(country.upper())}")
    if category:
        clauses.append(f"BusinessPartnerCategory eq {_q(category)}")
    if search:
        clauses.append(f"substringof({_q(search)},BusinessPartnerName)")
    try:
        return _read(client, "business_partner", top=top, filter_=" and ".join(clauses) or None,
                     select=",".join(SERVICES["business_partner"]["fields"]),
                     orderby="BusinessPartner")
    except Exception as exc:
        return _fail(exc)


def sap_get_business_partner(business_partner: str) -> dict:
    """Fetch one business partner by its SAP ID (e.g. 'BP0001')."""
    try:
        client = _client()
        started = time.time()
        item = client.get_entity("business_partner", business_partner,
                                 select=",".join(SERVICES["business_partner"]["fields"]))
        return {"ok": True, "system": _system(client), "count": 1, "items": [item],
                "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return _fail(exc)


def sap_list_sales_orders(top: int = 20, sold_to_party: Optional[str] = None,
                          customer_reference: Optional[str] = None) -> dict:
    """List sales orders (SD). Filter by customer ID or by the customer's own PO reference."""
    try:
        client = _client()
    except Exception as exc:
        return _fail(exc)
    clauses = []
    if sold_to_party:
        clauses.append(f"SoldToParty eq {_q(sold_to_party)}")
    if customer_reference:
        clauses.append(f"CustomerReference eq {_q(customer_reference)}")
    try:
        return _read(client, "sales_order", top=top, filter_=" and ".join(clauses) or None,
                     select=",".join(SERVICES["sales_order"]["fields"]),
                     orderby="SalesOrder")
    except Exception as exc:
        return _fail(exc)


def sap_list_purchase_orders(top: int = 20, supplier: Optional[str] = None,
                             company_code: Optional[str] = None) -> dict:
    """List purchase orders (MM) — the classic 'where is my PO / GR-IR mismatch' question."""
    try:
        client = _client()
    except Exception as exc:
        return _fail(exc)
    clauses = []
    if supplier:
        clauses.append(f"Supplier eq {_q(supplier)}")
    if company_code:
        clauses.append(f"CompanyCode eq {_q(company_code)}")
    try:
        return _read(client, "purchase_order", top=top, filter_=" and ".join(clauses) or None,
                     select=",".join(SERVICES["purchase_order"]["fields"]),
                     orderby="PurchaseOrder")
    except Exception as exc:
        return _fail(exc)


def sap_list_products(top: int = 20, product_type: Optional[str] = None,
                      product_group: Optional[str] = None) -> dict:
    """List materials/products (ProductType 'FERT'=finished, 'ROH'=raw, 'HAWA'=trading)."""
    try:
        client = _client()
    except Exception as exc:
        return _fail(exc)
    clauses = []
    if product_type:
        clauses.append(f"ProductType eq {_q(product_type.upper())}")
    if product_group:
        clauses.append(f"ProductGroup eq {_q(product_group)}")
    try:
        return _read(client, "product", top=top, filter_=" and ".join(clauses) or None,
                     select=",".join(SERVICES["product"]["fields"]), orderby="Product")
    except Exception as exc:
        return _fail(exc)


def sap_list_suppliers(top: int = 20, country: Optional[str] = None) -> dict:
    """List suppliers from A_Supplier, optionally filtered by country code."""
    try:
        client = _client()
    except Exception as exc:
        return _fail(exc)
    clauses = []
    if country:
        clauses.append(f"Country eq {_q(country.upper())}")
    try:
        return _read(client, "supplier", top=top, filter_=" and ".join(clauses) or None,
                     select=",".join(SERVICES["supplier"]["fields"]), orderby="Supplier")
    except Exception as exc:
        return _fail(exc)


def sap_odata_query(service: str, entity: Optional[str] = None, filter: Optional[str] = None,
                    select: Optional[str] = None, orderby: Optional[str] = None, top: int = 20) -> dict:
    """Escape hatch: run a READ-ONLY OData query against any whitelisted service.

    service: one of business_partner, sales_order, purchase_order, product, supplier.
    filter: a raw OData $filter string, e.g. "Country eq 'SA' and IsBlocked eq ''".
    """
    if service not in SERVICES:
        return {"ok": False, "error": {"error": "UNKNOWN_SERVICE", "message":
                f"'{service}' is not in the whitelist.", "allowed": sorted(SERVICES)}}
    try:
        client = _client()
        return _read(client, service, entity=entity, top=top, filter_=filter, select=select, orderby=orderby)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------- write tools


def sap_create_business_partner(business_partner: str, name: str, country: str = "SA",
                                category: str = "1", dry_run: bool = True) -> dict:
    """Create a business partner. dry_run=True (default) shows the exact payload without POSTing.

    A real write additionally requires SAP_WRITE_ENABLED=1 on the connector process —
    an agent can never flip that switch itself.
    """
    payload = {
        "BusinessPartner": business_partner,
        "BusinessPartnerCategory": category,
        "BusinessPartnerFullName": name,
        "BusinessPartnerName": name,
        "OrganizationBPName1": name,
        "Country": country.upper(),
    }
    try:
        client = _client()
    except Exception as exc:
        return _fail(exc)
    try:
        result = client.create_entity("business_partner", payload, dry_run=dry_run)
        return {"ok": True, "dry_run": bool(dry_run), "system": _system(client), "payload": payload,
                "result": result}
    except Exception as exc:
        return _fail(exc)


READ_TOOLS = [sap_health, sap_list_business_partners, sap_get_business_partner, sap_list_sales_orders,
              sap_list_purchase_orders, sap_list_products, sap_list_suppliers, sap_odata_query]
WRITE_TOOLS = [sap_create_business_partner]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
