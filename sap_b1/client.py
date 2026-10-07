"""Dependency-light SAP Business One Service Layer client (httpx only).

Why this exists: SAP Business One exposes a documented, session-cookie REST API
(Service Layer) at /b1s/v1 - no SAP licence beyond the B1 the client already owns,
no ABAP, no middleware. This client wraps the parts an agent needs:

  * login/logout and one automatic re-login when the server drops the session
  * $filter / $select / $top / $skip / $orderby / $count reads (read-first)
  * single-entity reads, create (POST) and update (PATCH)
  * a governance gate: any write needs config.write_enabled AND dry_run=False
  * every failure becomes B1Error / B1WriteBlocked with a JSON-safe dict
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from .config import B1Config, load_config

__all__ = ["B1Client", "B1Error", "B1WriteBlocked", "B1SessionError"]

_SESSION_CODES = {301, -1}          # Service Layer: invalid/expired session
_DUPLICATE_CODES = {-2028, -2035, -10}


class B1Error(Exception):
    """Any Service Layer failure, normalised for an agent to read."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[int] = None, operation: Optional[str] = None,
                 url: Optional[str] = None, details: Any = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.operation = operation
        self.url = url
        self.details = details
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "type": type(self).__name__,
                "message": str(self),
                "status": self.status,
                "code": self.code,
                "operation": self.operation,
                "url": self.url,
                "retryable": self.retryable,
                "details": self.details,
            },
        }


class B1WriteBlocked(B1Error):
    """Raised when a write was attempted while the governance gate is closed."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"Write blocked by the governance gate: {what} was not sent. "
            "Set SAP_B1_WRITE_ENABLED=1 (and get human approval) to allow writes."
        )


class B1SessionError(B1Error):
    """Login refused, or the session died and could not be re-established."""


def _envelope(payload: Any) -> tuple[Optional[int], Optional[str], Any]:
    """Pull (code, message, details) out of a Service Layer error body."""
    if not isinstance(payload, dict):
        return None, None, payload
    err = payload.get("error")
    if not isinstance(err, dict):
        return None, None, payload
    code = err.get("code")
    msg = err.get("message")
    if isinstance(msg, dict):
        msg = msg.get("value")
    details = err.get("message") if isinstance(err.get("message"), dict) else None
    return (code if isinstance(code, int) else None), (msg if isinstance(msg, str) else None), details


class B1Client:
    """One company connection. Cheap to construct; reuse one per process/tool call."""

    def __init__(self, config: Optional[B1Config] = None,
                 http_client: Optional[httpx.Client] = None) -> None:
        self.config = config or load_config()
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            verify=self.config.verify_tls, timeout=self.config.timeout,
            follow_redirects=False,
        )
        self._logged_in = False
        self._session: dict[str, Any] = {}

    # ------------------------------------------------------------------ auth
    def login(self) -> dict[str, Any]:
        if not self.config.configured:
            raise B1SessionError(
                "connector is not configured; missing: "
                + ", ".join(self.config.missing()),
                operation="login",
            )
        url = f"{self.config.session_url}/Login"
        body = {"CompanyDB": self.config.company_db, "UserName": self.config.username,
                "Password": self.config.password, "Language": self.config.language}
        try:
            resp = self._http.post(url, json=body)
        except httpx.HTTPError as exc:
            raise B1SessionError(f"login transport failure: {exc}", operation="login",
                                 url=url, retryable=True) from exc
        if resp.status_code >= 400:
            code, msg, details = _envelope(_safe_json(resp))
            raise B1SessionError(
                msg or f"login failed with HTTP {resp.status_code}", status=resp.status_code,
                code=code, operation="login", url=url, details=details)
        payload = _safe_json(resp) or {}
        self._session = payload if isinstance(payload, dict) else {}
        self._logged_in = True
        return {"ok": True, "session": {k: v for k, v in self._session.items()
                                       if k.lower() != "sessionid" or True},
                "system": self.config.redacted()}

    def logout(self) -> dict[str, Any]:
        if not self._logged_in:
            return {"ok": True, "note": "no active session"}
        url = f"{self.config.session_url}/Logout"
        try:
            resp = self._http.post(url)
        except httpx.HTTPError as exc:
            self._logged_in = False
            return {"ok": False, "error": {"type": "B1Error", "message": str(exc)}}
        self._logged_in = False
        return {"ok": resp.status_code < 400, "status": resp.status_code}

    def close(self) -> None:
        try:
            if self._logged_in:
                self.logout()
        finally:
            if self._owns_http:
                self._http.close()

    def __enter__(self) -> "B1Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None,
                 json_body: Any = None, retry_session: bool = True,
                 operation: Optional[str] = None) -> httpx.Response:
        if not self._logged_in:
            self.login()
        url = f"{self.config.session_url}/{path.lstrip('/')}"
        try:
            resp = self._http.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise B1Error(f"{method} {path} transport failure: {exc}", operation=operation,
                          url=url, retryable=True) from exc

        if resp.status_code < 400:
            return resp

        payload = _safe_json(resp)
        code, msg, details = _envelope(payload)
        expired = resp.status_code == 401 or code in _SESSION_CODES
        if expired and retry_session:
            self._logged_in = False
            self.login()                      # one retry, then give up
            return self._request(method, path, params=params, json_body=json_body,
                                 retry_session=False, operation=operation)
        raise B1Error(msg or f"{method} {path} failed with HTTP {resp.status_code}",
                      status=resp.status_code, code=code, operation=operation,
                      url=url, details=details,
                      retryable=resp.status_code in (408, 429, 500, 502, 503, 504))

    # ----------------------------------------------------------------- reads
    def count(self, entity: str, filter: Optional[str] = None) -> int:
        """Row count for a collection, or for the rows matching `filter` when given."""
        spec = self.config.entity(entity)
        params = {"$filter": filter} if filter else None
        resp = self._request("GET", f"{spec['path']}/$count", params=params,
                             operation="count")
        try:
            return int(str(resp.text).strip())
        except ValueError:
            raise B1Error(f"unexpected $count body from SAP B1: {resp.text[:120]!r}",
                          status=resp.status_code, operation="count") from None

    def query(self, entity: str, *, filter: Optional[str] = None,
              select: Optional[str] = None, top: int = 20, skip: int = 0,
              orderby: Optional[str] = None, count: bool = False) -> dict[str, Any]:
        spec = self.config.entity(entity)
        params: dict[str, Any] = {"$top": int(top)}
        if skip:
            params["$skip"] = int(skip)
        if filter:
            params["$filter"] = filter
        if orderby:
            params["$orderby"] = orderby
        if select:
            params["$select"] = select
        resp = self._request("GET", spec["path"], params=params, operation="query")
        payload = _safe_json(resp) or {}
        rows = payload.get("value")
        if not isinstance(rows, list):
            raise B1Error(f"unexpected Service Layer shape for {spec['path']}: "
                          f"{json.dumps(payload)[:180]}", status=resp.status_code,
                          operation="query")
        page: dict[str, Any] = {
            "ok": True,
            "system": {"label": self.config.label, "service_layer":
                       self.config.session_url, "company_db": self.config.company_db,
                       "shape": "b1-service-layer"},
            "entity": {"name": entity, "path": spec["path"], "key": spec["key"],
                       "label": spec["label"]},
            "items": rows,
            "row_count": len(rows),
            "top": int(top),
            "skip": int(skip),
        }
        if payload.get("@odata.nextLink"):
            page["odata_next_link"] = payload["@odata.nextLink"]
        if count:
            try:
                total = self.count(entity, filter=filter)
                page["count"] = total
                page["count_scope"] = "filtered" if filter else "collection"
            except B1Error as exc:                 # $count with a filter is not always allowed
                page["count"] = None
                page["count_scope"] = "unavailable"
                page["count_error"] = str(exc)
            if page.get("count") is not None:
                seen = int(skip) + len(rows)
                page["next"] = {"skip": seen} if seen < page["count"] else None
        return page

    def get_entity(self, entity: str, key: Any) -> dict[str, Any]:
        spec = self.config.entity(entity)
        path = f"{spec['path']}({_key_literal(key, spec['path'])})"
        resp = self._request("GET", path, operation="get")
        payload = _safe_json(resp) or {}
        return {"ok": True, "entity": entity, "key": str(key), "item": payload,
                "system": {"label": self.config.label, "company_db": self.config.company_db}}

    # ---------------------------------------------------------------- writes
    def create_entity(self, entity: str, payload: dict[str, Any], *,
                      dry_run: bool = True) -> dict[str, Any]:
        spec = self.config.entity(entity, write=True)
        missing = [f for f in spec.get("required", []) if not payload.get(f)]
        if missing:
            raise B1Error(f"missing required field(s) for {spec['label']}: {missing}",
                          operation="create")
        body = _whitelist(payload, spec.get("allowed", []))
        if not self.config.write_enabled:
            raise B1WriteBlocked(f"POST {spec['path']}")
        if dry_run:
            return {"ok": True, "dry_run": True, "executed": False,
                    "preview": {"method": "POST", "path": spec["path"], "body": body},
                    "note": "Governance gate is open; re-run with dry_run=False to send it."}
        resp = self._request("POST", spec["path"], json_body=body, operation="create")
        created = _safe_json(resp) or {}
        return {"ok": True, "dry_run": False, "executed": True, "entity": entity,
                "key": created.get(spec["key"], body.get(spec["key"])),
                "item": created, "status": resp.status_code,
                "system": {"label": self.config.label, "company_db": self.config.company_db}}

    def update_entity(self, entity: str, key: Any, payload: dict[str, Any], *,
                      dry_run: bool = True) -> dict[str, Any]:
        spec = self.config.entity(entity, write=True)
        body = _whitelist(payload, spec.get("allowed", []))
        if not body:
            raise B1Error("nothing to update: no allowed fields in payload", operation="update")
        if spec["key"] in body:
            raise B1Error(f"refusing to change the primary key {spec['key']} in an update",
                          operation="update")
        if not self.config.write_enabled:
            raise B1WriteBlocked(f"PATCH {spec['path']}({key})")
        path = f"{spec['path']}({_key_literal(key, spec['path'])})"
        if dry_run:
            return {"ok": True, "dry_run": True, "executed": False,
                    "preview": {"method": "PATCH", "path": path, "body": body},
                    "note": "Governance gate is open; re-run with dry_run=False to send it."}
        resp = self._request("PATCH", path, json_body=body, operation="update")
        updated = _safe_json(resp)
        return {"ok": True, "dry_run": False, "executed": True, "entity": entity,
                "key": str(key), "status": resp.status_code,
                "item": updated if isinstance(updated, dict) else None,
                "note": "PATCH accepted; verify with get_entity (read-back) for the audit trail."}

    # ---------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "system": self.config.redacted()}
        if not self.config.configured:
            out.update(ok=False, reachable=False,
                       error={"type": "B1SessionError",
                              "message": "not configured; missing: "
                                         + ", ".join(self.config.missing())})
            return out
        try:
            session = self.login()
            out["session"] = {k: v for k, v in session.get("session", {}).items()}
            out["reachable"] = True
            probe = self.query("business_partner", top=1, select="CardCode,CardName")
            out["probe"] = {"entity": "BusinessPartners", "row_count": probe["row_count"],
                            "sample": [r.get("CardCode") for r in probe["items"]]}
        except B1Error as exc:
            out.update(ok=False, reachable=False, error=exc.to_dict()["error"])
        return out


# ------------------------------------------------------------------- helpers
def _safe_json(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _whitelist(payload: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
    if not allowed:
        return dict(payload)
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise B1Error(f"field(s) not allowed for this entity: {unknown}; "
                      f"allowed: {allowed}", operation="write")
    return {k: v for k, v in payload.items() if v is not None}


def _key_literal(key: Any, path: str) -> str:
    """Service Layer keys are single-quoted strings for B1 (CardCode, ItemCode)."""
    text = str(key).replace("'", "''")
    return f"'{text}'"
