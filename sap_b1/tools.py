"""Agent-facing SAP Business One tools: thin, JSON-serialisable wrappers.

Same contract as the OData tools: every function returns a dict, never raises,
and writes are dry-run unless the caller passes dry_run=False *and* the process has
SAP_B1_WRITE_ENABLED=1 (the governance gate).
"""
from __future__ import annotations

from typing import Any, Optional

from .client import B1Client, B1Error
from .config import ENTITIES, WRITABLE_ENTITIES, B1Config, load_config

__all__ = [
    "b1_health", "b1_list_business_partners", "b1_get_business_partner",
    "b1_list_items", "b1_list_orders", "b1_list_invoices", "b1_list_purchase_orders",
    "b1_query", "b1_count", "b1_create_business_partner", "b1_update_business_partner",
]


def _client(config: Optional[B1Config] = None) -> B1Client:
    return B1Client(config or load_config())


def _q(value: str) -> str:
    """OData string literal with single quotes escaped."""
    return "'" + str(value).replace("'", "''") + "'"


def _fail(exc: Exception, *, service: Optional[str] = None) -> dict[str, Any]:
    if isinstance(exc, B1Error):
        out = exc.to_dict()
    else:
        out = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    if service:
        out["error"]["service"] = service
    return out


def _read(config: Optional[B1Config], entity: str, *, label: str, **kwargs: Any) -> dict[str, Any]:
    client = _client(config)
    try:
        return client.query(entity, **kwargs)
    except (B1Error, KeyError) as exc:
        return _fail(exc, service=label)
    finally:
        client.close()


# ------------------------------------------------------------------ diagnostics
def b1_health(config: Optional[B1Config] = None) -> dict[str, Any]:
    """Check the connector's configuration, login and a one-row read probe."""
    client = _client(config)
    try:
        return client.health()
    except Exception as exc:                      # never raise into the agent layer
        return _fail(exc)
    finally:
        client.close()


# ----------------------------------------------------------------------- reads
def b1_list_business_partners(top: int = 20, card_type: Optional[str] = None,
                              country: Optional[str] = None, search: Optional[str] = None,
                              count: bool = False,
                              config: Optional[B1Config] = None) -> dict[str, Any]:
    """List business partners (BusinessPartners). card_type: cCustomer, cSupplier or cLead."""
    clauses = []
    if card_type:
        clauses.append(f"CardType eq {_q(card_type)}")
    if country:
        clauses.append(f"Country eq {_q(country)}")
    if search:
        clauses.append(f"contains(CardName,{_q(search)})")
    return _read(config, "business_partner", label="SAP B1 BusinessPartners", top=top,
                 filter=" and ".join(clauses) or None,
                 select=",".join(ENTITIES["business_partner"]["fields"]),
                 orderby="CardCode", count=count)


def b1_get_business_partner(card_code: str,
                            config: Optional[B1Config] = None) -> dict[str, Any]:
    """Read one business partner by CardCode."""
    client = _client(config)
    try:
        return client.get_entity("business_partner", card_code)
    except (B1Error, KeyError) as exc:
        return _fail(exc, service="SAP B1 BusinessPartners")
    finally:
        client.close()


def b1_list_items(top: int = 20, search: Optional[str] = None, count: bool = False,
                  config: Optional[B1Config] = None) -> dict[str, Any]:
    """List items (Items), optionally filtering on the item name."""
    return _read(config, "item", label="SAP B1 Items", top=top,
                 filter=f"contains(ItemName,{_q(search)})" if search else None,
                 select=",".join(ENTITIES["item"]["fields"]), orderby="ItemCode", count=count)


def b1_list_orders(top: int = 20, card_code: Optional[str] = None,
                   count: bool = False,
                   config: Optional[B1Config] = None) -> dict[str, Any]:
    """List sales orders (Orders), optionally for one customer."""
    return _read(config, "order", label="SAP B1 Orders", top=top,
                 filter=f"CardCode eq {_q(card_code)}" if card_code else None,
                 select=",".join(ENTITIES["order"]["fields"]), orderby="DocEntry desc",
                 count=count)


def b1_list_invoices(top: int = 20, card_code: Optional[str] = None,
                     count: bool = False,
                     config: Optional[B1Config] = None) -> dict[str, Any]:
    """List A/R invoices (Invoices), optionally for one customer."""
    return _read(config, "invoice", label="SAP B1 Invoices", top=top,
                 filter=f"CardCode eq {_q(card_code)}" if card_code else None,
                 select=",".join(ENTITIES["invoice"]["fields"]), orderby="DocEntry desc",
                 count=count)


def b1_list_purchase_orders(top: int = 20, card_code: Optional[str] = None,
                            count: bool = False,
                            config: Optional[B1Config] = None) -> dict[str, Any]:
    """List purchase orders (PurchaseOrders), optionally for one vendor."""
    return _read(config, "purchase_order", label="SAP B1 PurchaseOrders", top=top,
                 filter=f"CardCode eq {_q(card_code)}" if card_code else None,
                 select=",".join(ENTITIES["purchase_order"]["fields"]),
                 orderby="DocEntry desc", count=count)


def b1_count(entity: str, config: Optional[B1Config] = None) -> dict[str, Any]:
    """Row count for one entity, read from the Service Layer $count endpoint."""
    client = _client(config)
    try:
        total = client.count(entity)
        return {"ok": True, "entity": entity, "count": total,
                "system": client.config.redacted()}
    except (B1Error, KeyError) as exc:
        return _fail(exc)
    finally:
        client.close()


def b1_query(entity: str, filter: Optional[str] = None, select: Optional[str] = None,
             top: int = 20, skip: int = 0, orderby: Optional[str] = None,
             count: bool = False, config: Optional[B1Config] = None) -> dict[str, Any]:
    """Generic READ-ONLY Service Layer query for advanced cases (raw OData syntax)."""
    if entity not in ENTITIES:
        return _fail(KeyError(f"unknown entity {entity!r}; known: {sorted(ENTITIES)}"),
                     service="sap-b1")
    return _read(config, entity, label=f"SAP B1 {ENTITIES[entity]['label']}", filter=filter,
                 select=select, top=top, skip=skip, orderby=orderby, count=count)


# ---------------------------------------------------------------------- writes
def b1_create_business_partner(card_code: str, card_name: str, card_type: str = "cCustomer",
                               dry_run: bool = True, country: Optional[str] = None,
                               currency: Optional[str] = None, email: Optional[str] = None,
                               phone: Optional[str] = None, group_code: Optional[int] = None,
                               config: Optional[B1Config] = None) -> dict[str, Any]:
    """Create a business partner. Dry-run by default; a real POST also needs the gate open."""
    payload = {"CardCode": card_code, "CardName": card_name, "CardType": card_type,
               "Country": country, "Currency": currency, "EmailAddress": email,
               "Phone1": phone, "GroupCode": group_code}
    payload = {k: v for k, v in payload.items() if v is not None}
    client = _client(config)
    try:
        result = client.create_entity("business_partner", payload, dry_run=dry_run)
        if result.get("executed"):
            readback = client.get_entity("business_partner",
                                        result.get("key") or card_code)
            result["read_back"] = readback.get("item")
            result["read_back_ok"] = readback["item"].get("CardCode") == card_code
        return result
    except (B1Error, KeyError) as exc:
        return _fail(exc, service="SAP B1 BusinessPartners")
    finally:
        client.close()


def b1_update_business_partner(card_code: str, dry_run: bool = True,
                               **fields: Any) -> dict[str, Any]:
    """Update a business partner (PATCH). Dry-run by default; a real PATCH needs the gate open."""
    allowed = WRITABLE_ENTITIES["business_partner"]["allowed"]
    clean = {k: v for k, v in fields.items() if v is not None}
    client = _client(config=None)
    try:
        result = client.update_entity("business_partner", card_code, clean, dry_run=dry_run)
        if result.get("executed"):
            readback = client.get_entity("business_partner", card_code)
            result["read_back"] = readback.get("item")
            result["read_back_ok"] = all(
                readback["item"].get(k) == v for k, v in clean.items())
        return result
    except (B1Error, KeyError) as exc:
        return _fail(exc, service=f"SAP B1 BusinessPartners (allowed fields: {allowed})")
    finally:
        client.close()
