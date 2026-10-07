"""Local mock of an SAP Business One Service Layer (dev / verification only).

Implements just enough of /b1s/v1 to exercise the connector honestly:
  POST /Login, POST /Logout, GET collection (+ $top/$skip/$filter/$select/$orderby),
  GET collection/$count, GET collection('key'), POST collection, PATCH collection('key').
Session auth is a real cookie check, so tests can drop the cookie and verify the
client's automatic re-login.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

MOCK_USER = os.environ.get("MOCK_B1_USER", "manager")
MOCK_PASSWORD = os.environ.get("MOCK_B1_PASSWORD", "mock-pass")
MOCK_COMPANY = os.environ.get("MOCK_B1_COMPANY", "SBODEMO")
COOKIE_NAME = "B1SESSION"

app = FastAPI(title="Mock SAP Business One Service Layer")

SESSIONS: dict[str, dict[str, Any]] = {}

CUSTOMERS = [
    ("C0001", "Gulf Medical Supplies LLC", "SA", "Jeddah", "SA",
     ["+966 12 555 0101"], "orders@gulfmedical.example", "SAR", 128500.50),
    ("C0002", "Riyadh Facilities Group", "SA", "Riyadh", "SA",
     ["+966 11 555 0202"], "ap@riyadhfacilities.example", "SAR", 48200.00),
    ("C0003", "Red Sea Industrial Co", "SA", "Yanbu", "SA",
     ["+966 14 555 0303"], "ops@redseaind.example", "SAR", 96310.75),
    ("C0004", "Dammam Power Services", "SA", "Dammam", "SA",
     ["+966 13 555 0404"], "info@dammampower.example", "SAR", 22150.00),
    ("C0005", "Jeddah Marine Contracting", "SA", "Jeddah", "SA",
     ["+966 12 555 0505"], "purchasing@jedmar.example", "SAR", 312000.00),
    ("C0006", "Al Khobar Medical Care", "SA", "Al Khobar", "SA",
     ["+966 13 555 0606"], "admin@khobarmed.example", "SAR", 74800.25),
    ("C0007", "Eastern Lift Maintenance", "SA", "Jubail", "SA",
     ["+966 13 555 0707"], "service@easternlift.example", "SAR", 15600.00),
    ("C0008", "Makkah Hotel Operations", "SA", "Makkah", "SA",
     ["+966 12 555 0808"], "finance@makkahhotels.example", "SAR", 208900.00),
    ("C0009", "Tabuk Water Systems", "SA", "Tabuk", "SA",
     ["+966 14 555 0909"], "projects@tabukwater.example", "SAR", 65400.40),
    ("C0010", "Abha Clinic Supplies", "SA", "Abha", "SA",
     ["+966 17 555 1010"], "orders@abhaclinic.example", "SAR", 18900.00),
    ("C0011", "Nexus Industrial Trading", "AE", "Dubai", "AE",
     ["+971 4 555 1111"], "sales@nexusind.example", "AED", 342100.00),
    ("C0012", "Levant Medical Imports", "JO", "Amman", "JO",
     ["+962 6 555 1212"], "imports@levantmed.example", "JOD", 27500.00),
]

SUPPLIERS = [
    ("V0001", "Siemens Healthcare Trading", "SA", "Riyadh", "SA", "SAR"),
    ("V0002", "Otis Elevator Arabia", "SA", "Jeddah", "SA", "SAR"),
    ("V0003", "Cummins Gulf Engines", "AE", "Dubai", "AE", "AED"),
    ("V0004", "Medical Gas Systems Ltd", "SA", "Riyadh", "SA", "SAR"),
    ("V0005", "ABB Power Products", "SA", "Dammam", "SA", "SAR"),
]

ITEM_DEFS = [
    ("IT0001", "Medical Gas Outlet Unit", "itInventory", 142, "EA"),
    ("IT0002", "Elevator Controller PCB", "itInventory", 23, "EA"),
    ("IT0003", "Diesel Generator 250kVA", "itInventory", 6, "EA"),
    ("IT0004", "LPG Bulk Tank 5T", "itInventory", 4, "EA"),
    ("IT0005", "Nurse Call Panel", "itInventory", 88, "EA"),
    ("IT0006", "FM Contract - Monthly", "itService", 0, "AU"),
    ("IT0007", "Annual Maintenance Visit", "itService", 0, "AU"),
    ("IT0008", "Air Filter Set", "itInventory", 310, "SET"),
    ("IT0009", "Bed Head Trunking", "itInventory", 57, "M"),
    ("IT0010", "Spare Parts Kit - HVAC", "itInventory", 19, "SET"),
]

ORDER_DEFS = [
    ("C0001", "2026-08-24", 48600.00, "bost_Open", 3),
    ("C0002", "2026-08-27", 12500.00, "bost_Open", 3),
    ("C0005", "2026-08-30", 142000.00, "bost_Open", 7),
    ("C0003", "2026-09-01", 33750.00, "bost_Open", 3),
    ("C0008", "2026-09-03", 89900.00, "bost_Close", 7),
    ("C0006", "2026-09-05", 21400.00, "bost_Open", 3),
    ("C0001", "2026-09-07", 5600.00, "bost_Close", 3),
    ("C0004", "2026-09-09", 33900.00, "bost_Open", 3),
]

INVOICE_DEFS = [
    ("C0008", "2026-09-03", 89900.00, "bost_Close", 89900.00),
    ("C0001", "2026-09-07", 5600.00, "bost_Close", 5600.00),
    ("C0005", "2026-09-02", 142000.00, "bost_Open", 0.00),
    ("C0002", "2026-08-28", 12500.00, "bost_Close", 12500.00),
    ("C0003", "2026-09-04", 33750.00, "bost_Open", 10000.00),
    ("C0011", "2026-09-06", 64200.00, "bost_Open", 0.00),
]

PO_DEFS = [
    ("V0001", "2026-08-20", 74000.00, "bost_Open"),
    ("V0002", "2026-08-26", 31500.00, "bost_Close"),
    ("V0004", "2026-09-02", 22800.00, "bost_Open"),
    ("V0003", "2026-09-08", 96500.00, "bost_Open"),
]


def _business_partners() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, name, country, city, region, phones, email, currency, balance in CUSTOMERS:
        rows.append({
            "CardCode": code, "CardName": name, "CardType": "cCustomer",
            "GroupCode": 100, "Phone1": phones[0], "EmailAddress": email,
            "City": city, "Country": country, "Currency": currency,
            "Balance": balance, "Valid": "tYES", "Notes": None,
            "ContactPerson": "Procurement", "Cellular": None,
        })
    for code, name, country, city, region, currency in SUPPLIERS:
        rows.append({
            "CardCode": code, "CardName": name, "CardType": "cSupplier",
            "GroupCode": 200, "Phone1": "+966 11 555 9999",
            "EmailAddress": f"{code.lower()}@vendor.example", "City": city,
            "Country": country, "Currency": currency, "Balance": 0.0,
            "Valid": "tYES", "Notes": None, "ContactPerson": "Sales desk",
            "Cellular": None,
        })
    return rows


def _items() -> list[dict[str, Any]]:
    return [{"ItemCode": code, "ItemName": name, "ItemType": kind,
             "ItemsGroupCode": 100, "QuantityOnStock": stock, "SalesUnit": unit,
             "PurchaseUnit": unit, "SalesItem": "tYES" if kind == "itService" else "tYES",
             "PurchaseItem": "tYES", "Valid": "tYES"}
            for code, name, kind, stock, unit in ITEM_DEFS]


def _docs(prefix: str, defs: list[tuple[Any, ...]], kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(defs):
        if kind == "order":
            card, date, total, status, salesperson = row
            out.append({"DocEntry": 100 + idx, "DocNum": 5100 + idx, "CardCode": card,
                        "CardName": _name_of(card), "DocDate": date, "DocDueDate": date,
                        "DocTotal": total, "DocumentStatus": status,
                        "SalesPersonCode": salesperson})
        elif kind == "invoice":
            card, date, total, status, paid = row
            out.append({"DocEntry": 300 + idx, "DocNum": 6100 + idx, "CardCode": card,
                        "CardName": _name_of(card), "DocDate": date, "DocTotal": total,
                        "DocumentStatus": status, "PaidToDate": paid})
        else:
            card, date, total, status = row
            out.append({"DocEntry": 500 + idx, "DocNum": 7100 + idx, "CardCode": card,
                        "CardName": _name_of(card), "DocDate": date, "DocDueDate": date,
                        "DocTotal": total, "DocumentStatus": status})
    return out


def _name_of(card_code: str) -> str:
    for row in _business_partners():
        if row["CardCode"] == card_code:
            return row["CardName"]
    return card_code


DATA: dict[str, list[dict[str, Any]]] = {
    "BusinessPartners": _business_partners(),
    "Items": _items(),
    "Orders": _docs("order", ORDER_DEFS, "order"),
    "Invoices": _docs("invoice", INVOICE_DEFS, "invoice"),
    "PurchaseOrders": _docs("po", PO_DEFS, "purchase_order"),
}

KEY_FIELDS = {"BusinessPartners": "CardCode", "Items": "ItemCode",
              "Orders": "DocEntry", "Invoices": "DocEntry", "PurchaseOrders": "DocEntry"}

REQUIRED = {"BusinessPartners": ["CardCode", "CardName", "CardType"],
            "Items": ["ItemCode", "ItemName", "ItemType"]}

# Properties the Service Layer accepts even when the current rows happen not to use them.
EXTRA_PROPS = {
    "BusinessPartners": {"CardName", "City", "Notes", "Cellular", "ContactPerson",
                         "Phone1", "EmailAddress", "Country", "Currency", "GroupCode",
                         "Valid", "Balance"},
    "Items": {"ItemName", "ItemsGroupCode", "QuantityOnStock", "SalesUnit",
              "PurchaseUnit", "SalesItem", "PurchaseItem", "Valid"},
}


def _err(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code,
                                           "message": {"lang": "en-us", "value": message}}})


def _session_id(req: Request) -> Optional[str]:
    return req.cookies.get(COOKIE_NAME)


def _require_session(req: Request) -> Optional[JSONResponse]:
    sid = _session_id(req)
    if not sid or sid not in SESSIONS:
        return _err(401, 301, "Invalid session or session already timeout.")
    return None


def _coerce(value: str, sample: Any) -> Any:
    text = value.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if isinstance(sample, bool) or text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _apply_filter(rows: list[dict[str, Any]], expr: str) -> list[dict[str, Any]]:
    def matches(row: dict[str, Any], clause: str) -> bool:
        clause = clause.strip()
        for fn, op in (("contains", "contains"), ("startswith", "startswith")):
            if clause.lower().startswith(fn + "(") and clause.endswith(")"):
                inner = clause[len(fn) + 1:-1]
                field, _, literal = inner.partition(",")
                field = field.strip()
                needle = literal.strip().strip("'").replace("''", "'")
                current = str(row.get(field, ""))
                return needle.lower() in current.lower() if op == "contains" \
                    else current.lower().startswith(needle.lower())
        for op in (" eq ", " ne ", " gt ", " ge ", " lt ", " le "):
            if op in clause:
                field, _, literal = clause.partition(op)
                field = field.strip()
                if field not in row:
                    return False
                want = _coerce(literal, row.get(field))
                got = row.get(field)
                try:
                    if op.strip() == "eq":
                        return got == want or str(got) == str(want)
                    if op.strip() == "ne":
                        return not (got == want or str(got) == str(want))
                    if op.strip() == "gt":
                        return got is not None and got > want
                    if op.strip() == "ge":
                        return got is not None and got >= want
                    if op.strip() == "lt":
                        return got is not None and got < want
                    if op.strip() == "le":
                        return got is not None and got <= want
                except TypeError:
                    return False
                return False
        return True

    for clause in expr.split(" and "):
        rows = [r for r in rows if matches(r, clause)]
    return rows


def _apply_order(rows: list[dict[str, Any]], orderby: str) -> list[dict[str, Any]]:
    for clause in reversed([c.strip() for c in orderby.split(",") if c.strip()]):
        bits = clause.split()
        field = bits[0]
        desc = len(bits) > 1 and bits[1].lower().startswith("desc")
        rows = sorted(rows, key=lambda r: (r.get(field) is None, r.get(field)), reverse=desc)
    return rows


@app.post("/b1s/v1/Login")
async def login(req: Request) -> Response:
    body = await req.json()
    if (str(body.get("CompanyDB", "")).strip().upper() != MOCK_COMPANY.upper()
            or body.get("UserName") != MOCK_USER
            or body.get("Password") != MOCK_PASSWORD):
        return _err(401, -1101, "Invalid company database or user name and password.")
    sid = "mock-b1-session"
    SESSIONS[sid] = {"company": body.get("CompanyDB"), "user": body.get("UserName")}
    resp = JSONResponse({"SessionId": sid, "Version": "1000180", "SessionTimeout": 30})
    resp.set_cookie(COOKIE_NAME, sid, httponly=True)
    return resp


@app.post("/b1s/v1/Logout")
async def logout(req: Request) -> Response:
    bad = _require_session(req)
    if bad:
        return bad
    sid = _session_id(req)
    SESSIONS.pop(sid, None)
    return Response(status_code=204)


@app.get("/b1s/v1/{collection}/$count")
async def collection_count(collection: str, req: Request) -> Response:
    bad = _require_session(req)
    if bad:
        return bad
    rows = DATA.get(collection)
    if rows is None:
        return _err(404, -1001, f"Collection {collection} does not exist.")
    if req.query_params.get("$filter"):
        rows = _apply_filter(list(rows), req.query_params["$filter"])
    return PlainTextResponse(str(len(rows)))


@app.get("/b1s/v1/{collection}")
async def collection_query(collection: str, req: Request) -> Response:
    bad = _require_session(req)
    if bad:
        return bad
    if "(" in collection:                  # BusinessPartners('C0001') lands here, not on its own
        head, _, tail = collection.partition("(")   # route: Starlette matches this one first
        if tail.endswith(")") and head in DATA:
            return await _read_single(head, tail[:-1], req)
        return _err(404, -1001, f"Collection {collection} does not exist.")
    rows = DATA.get(collection)
    if rows is None:
        return _err(404, -1001, f"Collection {collection} does not exist.")
    rows = list(rows)
    if req.query_params.get("$filter"):
        rows = _apply_filter(rows, req.query_params["$filter"])
    if req.query_params.get("$orderby"):
        rows = _apply_order(rows, req.query_params["$orderby"])
    if req.query_params.get("$select"):
        fields = [f.strip() for f in req.query_params["$select"].split(",") if f.strip()]
        rows = [{k: r.get(k) for k in fields} for r in rows]
    top = int(req.query_params.get("$top", 20))
    skip = int(req.query_params.get("$skip", 0))
    return JSONResponse({"@odata.context": f"/b1s/v1/$metadata#/{collection}",
                         "value": rows[skip:skip + top]})


async def _read_single(collection: str, key: str, req: Request) -> Response:
    """Single-entity read. Starlette registers the "/{collection}" route first, so this is
    reached via collection_query when the segment carries a ('KEY') suffix."""
    bad = _require_session(req)
    if bad:
        return bad
    rows = DATA.get(collection)
    if rows is None:
        return _err(404, -1001, f"Collection {collection} does not exist.")
    field = KEY_FIELDS.get(collection, "CardCode")
    wanted = key.strip().strip("'")
    for row in rows:
        if str(row.get(field)) == wanted:
            return JSONResponse(row)
    return _err(404, -5002, f"{collection.rstrip('s')} ({wanted}) does not exist.")


@app.post("/b1s/v1/{collection}")
async def collection_create(collection: str, req: Request) -> Response:
    bad = _require_session(req)
    if bad:
        return bad
    rows = DATA.get(collection)
    if rows is None:
        return _err(404, -1001, f"Collection {collection} does not exist.")
    body = await req.json()
    field = KEY_FIELDS.get(collection, "CardCode")
    for required in REQUIRED.get(collection, []):
        if not body.get(required):
            return _err(400, -5004, f"Required field {required} is missing.")
    if any(str(row.get(field)) == str(body.get(field)) for row in rows):
        return _err(400, -2028, f"{field} [{body.get(field)}] already exists.")
    row = {**body, field: body.get(field)}
    rows.append(row)
    return JSONResponse(row, status_code=201)


@app.patch("/b1s/v1/{collection}({key})")
async def collection_update(collection: str, key: str, req: Request) -> Response:
    bad = _require_session(req)
    if bad:
        return bad
    rows = DATA.get(collection)
    if rows is None:
        return _err(404, -1001, f"Collection {collection} does not exist.")
    field = KEY_FIELDS.get(collection, "CardCode")
    wanted = key.strip().strip("'")
    body = await req.json()
    for row in rows:
        if str(row.get(field)) == wanted:
            if field in body and str(body[field]) != wanted:
                return _err(400, -5005, f"{field} cannot be changed.")
            allowed_props = set().union(*(set(r) for r in rows)) | \
                EXTRA_PROPS.get(collection, set())
            unknown = [k for k in body if k not in allowed_props and k != field]
            if unknown:
                return _err(400, -5006, f"Invalid property: {unknown}")
            row.update(body)
            return Response(status_code=204)
    return _err(404, -5002, f"{collection.rstrip('s')} ({wanted}) does not exist.")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "mock-sap-b1-service-layer",
            "collections": sorted(DATA), "sessions": len(SESSIONS)}
