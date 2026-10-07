"""Local mock of the SAP Business Accelerator Hub OData sandbox.

Why this exists: the real sandbox (https://sandbox.api.sap.com) needs a free API
key tied to an SAP account, so the connector must be provable end-to-end without
it. This mock speaks the same dialect as SAP's gateway:

  * V2 collection shape:   {"d": {"results": [...], "__count": "N", "__next": "..."}}
  * V4 collection shape:   {"value": [...], "@odata.count": N, "@odata.nextLink": "..."}
  * API-key gate:          missing/incorrect `apikey` header -> 401 + fault envelope
                           (byte-shaped like SAP's, so error handling is exercised)
  * CSRF handshake:        GET with X-CSRF-Token: Fetch -> token, POST without it -> 403
  * $top / $skip / $filter / $select / $orderby / $inlinecount / $count / single-key read

Run:  uvicorn mock_sap.server:app --host 127.0.0.1 --port 8792
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MOCK_APIKEY = os.environ.get("MOCK_SAP_APIKEY", "local-dev-key")
CSRF_TOKEN = "mock-csrf-token-0001"

app = FastAPI(title="Mock SAP OData Sandbox", version="0.1.0")

# --------------------------------------------------------------------- data


def _business_partners() -> list[dict[str, Any]]:
    rows = []
    countries = ["SA", "AE", "IN", "DE", "EG", "PK"]
    names = ["Al Rajhi Industrial", "Gulf Mechanical Contracting", "Zameer Trading Est.",
             "Red Sea Facilities Co.", "Jeddah Medical Gas LLC", "Hyderabad Elevators Pvt",
             "Riyadh HVAC Solutions", "Yanbu Power Services", "Dammam Steel Works",
             "Oryx Maintenance Group", "Nile Engineering", "Sahara Logistics",
             "Karachi Pumps Ltd", "Munich Automation GmbH", "Cairo Electromechanical",
             "Tabuk Infrastructure", "Mecca Water Systems", "Abu Dhabi Generators",
             "Bahrain Cooling Co.", "Muscat Facilities LLC", "Khobar Glass Works",
             "Suez Fire Protection", "Lahore Instruments", "Berlin Lifts GmbH",
             "Alexandria HVAC Supply"]
    for idx, name in enumerate(names, start=1):
        rows.append({
            "BusinessPartner": f"BP{idx:04d}",
            "BusinessPartnerCategory": "1" if idx % 4 else "2",  # 1 = organization, 2 = person
            "BusinessPartnerFullName": name,
            "BusinessPartnerName": name,
            "OrganizationBPName1": name,
            "SearchTerm1": name.split()[0].upper(),
            "Country": countries[idx % len(countries)],
            "Region": "01" if idx % 2 else "02",
            "CreationDate": f"/Date({1600000000000 + idx * 86400000})/",
            "IsBlocked": "X" if idx % 11 == 0 else "",
        })
    return rows


def _sales_orders() -> list[dict[str, Any]]:
    rows = []
    statuses = ["A", "B", "C"]
    for idx in range(1, 13):
        rows.append({
            "SalesOrder": f"{10000000 + idx}",
            "SalesOrderType": "OR" if idx % 2 else "RE",
            "SoldToParty": f"BP{(idx % 25) + 1:04d}",
            "CustomerReference": f"PO-CUST-{idx:03d}",
            "CreationDate": f"/Date({1700000000000 + idx * 43200000})/",
            "TotalNetAmount": f"{12000 + idx * 375}.00",
            "OverallSDProcessStatus": statuses[idx % len(statuses)],
            "SalesOrganization": "1000" if idx % 2 else "2000",
            "TransactionCurrency": "SAR",
        })
    return rows


def _purchase_orders() -> list[dict[str, Any]]:
    rows = []
    for idx in range(1, 9):
        rows.append({
            "PurchaseOrder": f"{4500000000 + idx}",
            "Supplier": f"BP{(idx * 3) % 25 + 1:04d}",
            "CompanyCode": "1000",
            "PurchaseOrderDate": f"/Date({1702000000000 + idx * 86400000})/",
            "PurchasingGroup": "001",
            "PurchasingDocumentCategory": "F",
            "DocumentCurrency": "SAR",
            "PurchaseOrderNetAmount": f"{5000 + idx * 1250}.00",
            "PurchasingProcessingStatus": "02" if idx % 3 else "05",
        })
    return rows


def _products() -> list[dict[str, Any]]:
    rows = []
    names = ["Copper Pipe 15mm", "Medical Gas Outlet", "Elevator Door Sensor", "Diesel Generator 250kVA",
             "LPG Regulator", "AHU Filter G4", "Fire Damper 300mm", "Pressure Gauge 0-16bar",
             "Cable Tray 2m", "Cooling Coil CU-120"]
    for idx, name in enumerate(names, start=1):
        rows.append({
            "Product": f"MAT-{idx:04d}",
            "ProductType": "FERT" if idx % 3 == 0 else "HAWA",
            "BaseUnit": "PC",
            "ProductGroup": "001" if idx % 2 else "002",
            "GrossWeight": f"{2.5 * idx:.3f}",
            "NetWeight": f"{2.25 * idx:.3f}",
            "CreationDate": f"/Date({1650000000000 + idx * 86400000})/",
            "ProductDescription": name,
        })
    return rows


def _suppliers() -> list[dict[str, Any]]:
    """A_Supplier view of the same master data (SAP's supplier entity names differ)."""
    return [{"Supplier": r["BusinessPartner"], "SupplierName": r["BusinessPartnerName"],
             "Country": r["Country"], "Region": r["Region"], "CreationDate": r["CreationDate"],
             "IsBlocked": r["IsBlocked"]} for r in _business_partners()]


DATA: dict[str, list[dict[str, Any]]] = {
    "A_BusinessPartner": _business_partners(),
    "A_Supplier": _suppliers(),
    "A_SalesOrder": _sales_orders(),
    "A_PurchaseOrder": _purchase_orders(),
    "A_Product": _products(),
}

# --------------------------------------------------------------- OData query


def _parse_literal(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 8 and raw.startswith("/Date("):
        inner = raw[6:-2].split("+")[0].split("-")[0] if not raw[6:-2].startswith("-") else raw[6:-2]
        return inner
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _coerce(field_value: Any, literal: Any) -> Any:
    if isinstance(field_value, str) and not isinstance(literal, str):
        text = str(field_value).strip()
        if text.startswith("/Date("):
            digits = re.sub(r"\D", "", text)
            try:
                return float(digits)
            except ValueError:
                return field_value
        try:
            return float(text)
        except ValueError:
            return field_value
    return field_value


def _eval_condition(row: dict[str, Any], cond: str) -> bool:
    cond = cond.strip()
    m = re.match(r"^startswith\((\w+),\s*'([^']*)'\)$", cond, re.I)
    if m:
        return str(row.get(m.group(1), "")).lower().startswith(m.group(2).lower())
    m = re.match(r"^substringof\('([^']*)',\s*(\w+)\)$", cond, re.I)
    if m:
        return m.group(1).lower() in str(row.get(m.group(2), "")).lower()
    m = re.match(r"^contains\((\w+),\s*'([^']*)'\)$", cond, re.I)
    if m:
        return m.group(2).lower() in str(row.get(m.group(1), "")).lower()
    m = re.match(r"^(\w+)\s+(eq|ne|gt|ge|lt|le)\s+(.+)$", cond, re.I)
    if not m:
        raise ValueError(f"unsupported $filter expression: {cond!r}")
    field, op, raw = m.group(1), m.group(2).lower(), m.group(3)
    literal = _parse_literal(raw)
    actual = _coerce(row.get(field), literal)
    if isinstance(actual, str) or isinstance(literal, str):
        actual, literal = str(actual), str(literal)
    elif isinstance(actual, float) or isinstance(literal, float):
        try:
            actual, literal = float(actual), float(literal)
        except (TypeError, ValueError):
            pass
    table = {"eq": actual == literal, "ne": actual != literal, "gt": actual > literal,
             "ge": actual >= literal, "lt": actual < literal, "le": actual <= literal}
    return table[op]


def _apply_filter(rows: list[dict[str, Any]], expr: str) -> list[dict[str, Any]]:
    # SQL-92/SAP-style: split on ' and ' at top level (no parentheses nesting support)
    parts = [p for p in re.split(r"\s+and\s+", expr, flags=re.I) if p.strip()]
    out = rows
    for part in parts:
        out = [r for r in out if _eval_condition(r, part)]
    return out


def _apply_orderby(rows: list[dict[str, Any]], expr: str) -> list[dict[str, Any]]:
    for token in reversed([t.strip() for t in expr.split(",") if t.strip()]):
        bits = token.split()
        field = bits[0]
        desc = len(bits) > 1 and bits[1].lower() == "desc"
        rows = sorted(rows, key=lambda r, f=field: str(r.get(f, "")), reverse=desc)
    return rows


def _apply_select(rows: list[dict[str, Any]], fields: str) -> list[dict[str, Any]]:
    want = [f.strip() for f in fields.split(",") if f.strip()]
    return [{k: v for k, v in row.items() if k in want} for row in rows]


def _odata_json(payload: Any, req: Request) -> JSONResponse:
    return JSONResponse(payload, headers={"X-CSRF-Token": CSRF_TOKEN} if _csrf_fetch(req) else None)


def _csrf_fetch(req: Request) -> bool:
    return (req.headers.get("x-csrf-token") or "").lower() == "fetch"


def _key_check(req: Request) -> Optional[JSONResponse]:
    if (req.headers.get("apikey") or "") != MOCK_APIKEY:
        return JSONResponse(status_code=401, content={"fault": {
            "faultstring": "Failed to resolve API Key variable request.header.apikey",
            "detail": {"errorcode": "steps.oauth.v2.FailedToResolveAPIKey"}}})
    return None


def _respond(req: Request, rows: list[dict[str, Any]], shape: str,
             entity: str, service: str, extra: Optional[dict[str, Any]] = None) -> JSONResponse:
    params = dict(req.query_params)
    try:
        if params.get("$filter"):
            rows = _apply_filter(rows, params["$filter"])
        if params.get("$orderby"):
            rows = _apply_orderby(rows, params["$orderby"])
        if params.get("$select"):
            rows = _apply_select(rows, params["$select"])
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {"code": "/IWFND/MED/012", "message": {"lang": "en",
                            "value": f"Invalid query option: {exc}"}}})
    total = len(rows)
    skip = int(params.get("$skip") or 0)
    top = int(params.get("$top") or 0)
    window = rows[skip: skip + top] if top else rows[skip:]
    if extra:
        for key, value in extra.items():
            for row in window:
                row.setdefault(key, value)
    has_more = bool(top) and (skip + top) < total
    if shape == "v4":
        base = str(req.url).split("?")[0]
        payload: dict[str, Any] = {"@odata.context": f"{base}/$metadata#{entity}", "value": window,
                                   "@odata.count": total}
        if has_more:
            payload["@odata.nextLink"] = f"{base}?$skip={skip + top}&$top={top}"
            if params.get("$filter"):
                payload["@odata.nextLink"] += f"&$filter={params['$filter']}"
    else:
        payload = {"d": {"results": window}}
        if params.get("$inlinecount") == "allpages":
            payload["d"]["__count"] = str(total)
        if has_more:
            next_url = f"{str(req.url).split('?')[0]}?$skip={skip + top}&$top={top}"
            if params.get("$filter"):
                next_url += f"&$filter={params['$filter']}"
            payload["d"]["__next"] = next_url
    return _odata_json(payload, req)


# ------------------------------------------------------------------ routes

@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "mock-sap-odata", "entities": sorted(DATA),
            "apikey_required": True, "rows": {k: len(v) for k, v in DATA.items()}}


def _match_entity(path_items: list[str], shape: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (service, entity, key) from the URL tail."""
    for idx, part in enumerate(path_items):
        m = re.match(r"^([A-Za-z0-9_]+)(\(.*\))?$", part)
        base = m.group(1) if m else part
        if base not in DATA:
            continue
        key = None
        suffix = m.group(2) if m else None
        if suffix:
            km = re.match(r"^\((?:'([^']*)'|(\d+))\)$", suffix)
            if km:
                key = km.group(1) if km.group(1) is not None else km.group(2)
        elif idx + 1 < len(path_items):
            km = re.match(r"^\((?:'([^']*)'|(\d+))\)(?:/.*)?$", path_items[idx + 1])
            if km:
                key = km.group(1) if km.group(1) is not None else km.group(2)
        service = path_items[idx - 1] if idx > 0 else ("odata4" if shape == "v4" else "odata")
        return service, base, key
    return None, None, None


@app.get("/{full_path:path}")
def odata_get(full_path: str, req: Request) -> JSONResponse:
    gate = _key_check(req)
    if gate is not None:
        return gate
    parts = [p for p in full_path.split("/") if p]
    shape = "v4" if "odata4" in parts else "v2"
    service, entity, key = _match_entity(parts, shape)
    if entity is None:
        if full_path.endswith("$metadata"):
            return JSONResponse(status_code=200, content={"$metadata": "mock", "note": "metadata not modelled"},
                                media_type="application/xml")
        return JSONResponse(status_code=404, content={"error": {"code": "/IWFND/MED/027",
                            "message": {"lang": "en", "value": f"Resource not found: /{full_path}"}}})
    rows = DATA[entity]
    if key is None:
        return _respond(req, [dict(r) for r in rows], shape, entity, service or "")
    hit = [r for r in rows if str(r.get(entity.replace("A_", ""))) == str(key)
           or str(r.get("BusinessPartner")) == str(key) or str(r.get("Supplier")) == str(key)
           or str(r.get("SalesOrder")) == str(key) or str(r.get("PurchaseOrder")) == str(key)
           or str(r.get("Product")) == str(key)]
    if not hit:
        return JSONResponse(status_code=404, content={"error": {"code": "/IWFND/MED/027",
                            "message": {"lang": "en", "value": f"No entity found for key {key}"}}})
    row = dict(hit[0])
    if req.query_params.get("$select"):
        row = _apply_select([row], req.query_params["$select"])[0]
    if shape == "v4":
        return _odata_json({"@odata.context": str(req.url).split("?")[0], **row}, req)
    return _odata_json({"d": row}, req)


@app.post("/{full_path:path}")
async def odata_post(full_path: str, req: Request) -> JSONResponse:
    gate = _key_check(req)
    if gate is not None:
        return gate
    parts = [p for p in full_path.split("/") if p]
    shape = "v4" if "odata4" in parts else "v2"
    _, entity, _ = _match_entity(parts, shape)
    if entity is None:
        return JSONResponse(status_code=404, content={"error": {"code": "/IWFND/MED/027",
                            "message": {"lang": "en", "value": "Collection not found"}}})
    if (req.headers.get("x-csrf-token") or "") not in {CSRF_TOKEN, "mock-csrf-token-0001"}:
        return JSONResponse(status_code=403, content={"error": {"code": "/IWFND/MED/194", "message": {
            "lang": "en", "value": "CSRF token validation failed"}}})
    payload = await req.json()
    key_field = {"A_BusinessPartner": "BusinessPartner", "A_Supplier": "Supplier",
                 "A_SalesOrder": "SalesOrder", "A_PurchaseOrder": "PurchaseOrder",
                 "A_Product": "Product"}[entity]
    if not payload.get(key_field):
        return JSONResponse(status_code=400, content={"error": {"code": "MOCK/001", "message": {
            "lang": "en", "value": f"Mandatory field {key_field} missing"}}})
    if any(str(r.get(key_field)) == str(payload[key_field]) for r in DATA[entity]):
        return JSONResponse(status_code=409, content={"error": {"code": "/IWBEP/CX_MGW_BUSI_EXCEPTION", "message": {
            "lang": "en", "value": f"{key_field} {payload[key_field]} already exists"}}})
    row = dict(payload)
    row.setdefault("CreationDate", "/Date(1757500000000)/")
    DATA[entity].append(row)
    location = f"{str(req.url).split('?')[0]}('{row[key_field]}')"
    if shape == "v4":
        return JSONResponse(status_code=201, content=row, headers={"Location": location})
    return JSONResponse(status_code=201, content={"d": row}, headers={"Location": location})
