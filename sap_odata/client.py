"""Dependency-light SAP OData client (V2 + V4) for the connector layer.

Design rules that matter for SAP:
  * Never raise raw HTTP noise at the agent -> every failure becomes SapODataError
    with SAP's own error code/message plus a `retryable` flag.
  * Reads are open; writes need config.write_enabled (default False) so an agent
    can never create a business object in a client system without a human gate.
  * Paging is explicit: callers see the page URL and the next-page token, and
    query_all() has a hard page cap (no accidental full-table scans).
"""
from __future__ import annotations

import random
import time
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from .config import SERVICES, SapConfig


class SapODataError(Exception):
    """Normalised SAP error (V2 error envelope, V4 error envelope, or API-key fault)."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None,
                 url: Optional[str] = None, method: Optional[str] = None, details: Any = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.url = url
        self.method = method
        self.details = details
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        details = self.details
        if details is not None and not isinstance(details, (dict, list, str, int, float, bool)):
            details = str(details)
        return {"error": self.code or "SAP_ODATA_ERROR", "message": self.message, "http_status": self.status,
                "method": self.method, "url": self.url, "retryable": self.retryable, "details": details}


class SapWriteBlocked(PermissionError):
    """Raised when a write is attempted while config.write_enabled is False."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"Write operation blocked by governance gate: {what}. "
            "Set SAP_WRITE_ENABLED=1 (and get human approval) to allow SAP writes."
        )
        self.what = what

    def to_dict(self) -> dict[str, Any]:
        return {"error": "SAP_WRITE_BLOCKED", "message": str(self), "retryable": False,
                "hint": "Read-only mode is the default. Writes require SAP_WRITE_ENABLED=1 plus an approval step."}


_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SapODataClient:
    """Small, honest OData client for SAP S/4HANA, ECC (Gateway) and the ABAP sandbox."""

    def __init__(self, config: SapConfig) -> None:
        self.config = config
        self._client = httpx.Client(verify=config.verify_tls, timeout=config.timeout, follow_redirects=False)
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.last_call: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ auth
    def _oauth_token(self) -> str:
        cfg = self.config
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        if not (cfg.oauth_token_url and cfg.oauth_client_id and cfg.oauth_client_secret):
            raise SapODataError("auth_type=oauth2 but oauth_token_url / client id / secret are not configured",
                                code="SAP_AUTH_MISCONFIGURED")
        resp = self._client.post(cfg.oauth_token_url, data={"grant_type": "client_credentials"},
                                 auth=(cfg.oauth_client_id, cfg.oauth_client_secret),
                                 headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise SapODataError("OAuth token request failed", status=resp.status_code, code="SAP_OAUTH_FAILED",
                                url=cfg.oauth_token_url, method="POST", details=resp.text[:400], retryable=False)
        body = resp.json()
        self._token = body.get("access_token")
        self._token_expires_at = time.time() + float(body.get("expires_in", 3600))
        if not self._token:
            raise SapODataError("OAuth response contained no access_token", code="SAP_OAUTH_FAILED",
                                details=body)
        return self._token

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        cfg = self.config
        headers: dict[str, str] = {"Accept": "application/json"}
        if cfg.api_key:
            headers["apikey"] = cfg.api_key
        if cfg.auth_type == "basic":
            if not (cfg.username and cfg.password):
                raise SapODataError("auth_type=basic but username/password missing", code="SAP_AUTH_MISCONFIGURED")
            import base64
            token = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        elif cfg.auth_type == "oauth2":
            headers["Authorization"] = f"Bearer {self._oauth_token()}"
        if cfg.sap_client:
            headers["sap-client"] = cfg.sap_client
        if cfg.extra_headers:
            headers.update(cfg.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    # --------------------------------------------------------------- request
    def request(self, method: str, url: str, *, params: Optional[dict[str, Any]] = None,
                json_body: Any = None, headers: Optional[dict[str, str]] = None,
                expect_json: bool = True) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            started = time.time()
            try:
                resp = self._client.request(method.upper(), url, params=params, json=json_body,
                                            headers=self._headers(headers))
            except httpx.HTTPError as exc:
                if attempt > self.config.max_retries:
                    raise SapODataError(f"Transport error after {attempt} attempts: {exc}", code="SAP_TRANSPORT_ERROR",
                                        url=url, method=method, retryable=True) from exc
                self._sleep_backoff(attempt, None)
                continue
            self.last_call = {"method": method.upper(), "url": str(resp.request.url),
                              "status": resp.status_code, "elapsed_ms": int((time.time() - started) * 1000)}
            if resp.status_code < 400:
                return resp
            retryable = resp.status_code in _RETRY_STATUS
            if retryable and attempt <= self.config.max_retries:
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                continue
            raise self._error_from_response(resp, method, url)

    @staticmethod
    def _sleep_backoff(attempt: int, retry_after: Optional[str]) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 10.0))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(0.4 * (2 ** (attempt - 1)), 5.0) + random.uniform(0, 0.25))

    @staticmethod
    def _error_from_response(resp: httpx.Response, method: str, url: str) -> SapODataError:
        code, message, details = None, (resp.text or "").strip()[:400] or resp.reason_phrase, None
        try:
            body = resp.json()
        except Exception:  # non-JSON (HTML error page, gateway timeout)
            body = None
        if isinstance(body, dict):
            err = body.get("error")
            fault = body.get("fault")
            if isinstance(err, dict):
                code = err.get("code")
                msg = err.get("message")
                if isinstance(msg, dict):
                    message = msg.get("value") or message
                elif isinstance(msg, str):
                    message = msg
                details = err.get("innererror") or err.get("details")
            elif isinstance(fault, dict):
                code = fault.get("faultstring") or "SAP_API_GATEWAY_FAULT"
                message = fault.get("faultstring") or message
                details = fault.get("detail")
        return SapODataError(f"SAP {resp.status_code}: {message}", status=resp.status_code, code=code,
                             url=url, method=method, details=details, retryable=resp.status_code in _RETRY_STATUS)

    # ---------------------------------------------------------------- paging
    @staticmethod
    def _shape(payload: Any) -> tuple[list[dict[str, Any]], Optional[int], Optional[str], str]:
        """Normalise a V2 or V4 collection payload -> (items, count, next_link, shape)."""
        if not isinstance(payload, dict):
            return [], None, None, "unknown"
        count: Optional[int] = None
        next_link: Optional[str] = None
        if isinstance(payload.get("d"), dict):
            d = payload["d"]
            items = d.get("results") if isinstance(d.get("results"), list) else []
            raw_count = d.get("__count")
            count = int(raw_count) if str(raw_count or "").isdigit() else None
            next_link = d.get("__next")
            return items, count, next_link, "v2"
        if isinstance(payload.get("d"), list):
            return payload["d"], None, None, "v2"
        if isinstance(payload.get("value"), list):
            raw_count = payload.get("@odata.count")
            count = int(raw_count) if str(raw_count or "").isdigit() else None
            return payload["value"], count, payload.get("@odata.nextLink"), "v4"
        return [], None, None, "unknown"

    # ----------------------------------------------------------------- reads
    def query(self, service_key: str, *, top: Optional[int] = None, skip: int = 0,
              filter: Optional[str] = None, select: Optional[list[str] | str] = None,
              orderby: Optional[str] = None, count: bool = False,
              entity: Optional[str] = None) -> dict[str, Any]:
        spec = SERVICES.get(service_key)
        if spec is None:
            raise SapODataError(f"Service '{service_key}' is not whitelisted (available: {sorted(SERVICES)})",
                                code="SAP_SERVICE_NOT_ALLOWED", retryable=False)
        entity_name = entity or spec["entity"]
        url = f"{self.config.service_base(spec['service'])}/{entity_name}"
        params: dict[str, Any] = {}
        top = self.config.page_size if top is None else top
        if top:
            params["$top"] = int(top)
        if skip:
            params["$skip"] = int(skip)
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = ",".join(select) if isinstance(select, (list, tuple)) else select
        if orderby:
            params["$orderby"] = orderby
        version = self.config.odata_version
        if count:
            params["$count" if version == "v4" else "$inlinecount"] = "true" if version == "v4" else "allpages"
        try:
            resp = self.request("GET", url, params=params)
        except SapODataError as exc:
            # gateways differ: retry once with the other count/paging dialect
            if count and exc.status == 400:
                params.pop("$inlinecount", None)
                params.pop("$count", None)
                params["$count" if "v4" in self.config.odata_path else "$inlinecount"] = \
                    "true" if "v4" in self.config.odata_path else "allpages"
                resp = self.request("GET", url, params=params)
            else:
                raise
        items, total, next_link, shape = self._shape(resp.json())
        return {"ok": True, "service": service_key, "entity": entity_name, "shape": shape, "count": total,
                "returned": len(items), "next": next_link, "url": str(resp.request.url), "items": items}

    def query_all(self, service_key: str, *, filter: Optional[str] = None,
                  select: Optional[list[str] | str] = None, orderby: Optional[str] = None,
                  page_size: Optional[int] = None, max_pages: Optional[int] = None) -> dict[str, Any]:
        page_size = page_size or self.config.page_size
        max_pages = max_pages or self.config.max_pages
        collected: list[dict[str, Any]] = []
        pages = 0
        skip = 0
        truncated = False
        url: Optional[str] = None
        while pages < max_pages:
            page = self.query(service_key, top=page_size, skip=skip, filter=filter, select=select,
                              orderby=orderby, count=(pages == 0))
            pages += 1
            collected.extend(page["items"])
            url = page["url"]
            next_link = page.get("next")
            if next_link:
                resp = self.request("GET", urljoin(page["url"], next_link) if not str(next_link).startswith("http") else next_link)
                items, _, next_link2, _ = self._shape(resp.json())
                collected.extend(items)
                pages += 1
                skip = len(collected)
                if not next_link2 and len(items) < page_size:
                    break
                continue
            if len(page["items"]) < page_size:
                break
            skip += page_size
        else:
            truncated = True
        return {"ok": True, "service": service_key, "pages": pages, "returned": len(collected),
                "truncated": truncated, "last_url": url, "items": collected,
                "hint": "Increase max_pages/page_size for a full extract, or add a $filter." if truncated else None}

    def get_entity(self, service_key: str, key_value: str, *, select: Optional[list[str] | str] = None,
                   entity: Optional[str] = None) -> dict[str, Any]:
        spec = SERVICES.get(service_key)
        if spec is None:
            raise SapODataError(f"Service '{service_key}' is not whitelisted", code="SAP_SERVICE_NOT_ALLOWED")
        entity_name = entity or spec["entity"]
        escaped = str(key_value).replace("'", "''")
        url = f"{self.config.service_base(spec['service'])}/{entity_name}('{escaped}')"
        params: dict[str, Any] = {}
        if select:
            params["$select"] = ",".join(select) if isinstance(select, (list, tuple)) else select
        resp = self.request("GET", url, params=params or None)
        payload = resp.json()
        if isinstance(payload.get("d"), dict):
            return {"ok": True, "service": service_key, "key": key_value, "url": str(resp.request.url),
                    "item": payload["d"]}
        return {"ok": True, "service": service_key, "key": key_value, "url": str(resp.request.url), "item": payload}

    def generic_query(self, service_name: str, entity_name: str, **kwargs: Any) -> dict[str, Any]:
        """Read-only escape hatch for a whitelisted *service* but any entity under it."""
        spec = {k: v for k, v in SERVICES.items() if v["service"] == service_name}
        if not spec:
            raise SapODataError(f"Service '{service_name}' is not whitelisted (available services: "
                                f"{sorted({v['service'] for v in SERVICES.values()})})",
                                code="SAP_SERVICE_NOT_ALLOWED")
        url = f"{self.config.service_base(service_name)}/{entity_name}"
        params: dict[str, Any] = {}
        for key in ("$top", "$skip", "$filter", "$select", "$orderby"):
            value = kwargs.get(key)
            if value not in (None, ""):
                params[key] = value
        resp = self.request("GET", url, params=params or None)
        items, total, next_link, shape = self._shape(resp.json())
        return {"ok": True, "service": service_name, "entity": entity_name, "shape": shape, "count": total,
                "returned": len(items), "next": next_link, "url": str(resp.request.url), "items": items}

    # ---------------------------------------------------------------- writes
    def fetch_csrf_token(self, service_key: str, entity: Optional[str] = None) -> str:
        spec = SERVICES.get(service_key)
        if spec is None:
            raise SapODataError(f"Service '{service_key}' is not whitelisted", code="SAP_SERVICE_NOT_ALLOWED")
        entity_name = entity or spec["entity"]
        url = f"{self.config.service_base(spec['service'])}/{entity_name}?$top=1"
        resp = self.request("GET", url, headers={"X-CSRF-Token": "Fetch", "X-Requested-With": "XMLHttpRequest"})
        token = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token")
        if not token:
            raise SapODataError("SAP did not return an X-CSRF-Token (CSRF disabled or session not established)",
                                code="SAP_CSRF_MISSING", url=url, retryable=False)
        return token

    def create_entity(self, service_key: str, payload: dict[str, Any], *, entity: Optional[str] = None,
                      dry_run: bool = True) -> dict[str, Any]:
        spec = SERVICES.get(service_key)
        if spec is None:
            raise SapODataError(f"Service '{service_key}' is not whitelisted", code="SAP_SERVICE_NOT_ALLOWED")
        entity_name = entity or spec["entity"]
        url = f"{self.config.service_base(spec['service'])}/{entity_name}"
        if dry_run:
            return {"ok": True, "dry_run": True, "service": service_key, "entity": entity_name, "url": url,
                    "payload": payload, "executed": False,
                    "note": "Nothing was sent to SAP. Re-run with dry_run=false to actually create the object."}
        if not self.config.write_enabled:
            raise SapWriteBlocked(f"POST {url}")
        token = self.fetch_csrf_token(service_key, entity=entity_name)
        resp = self.request("POST", url, json_body=payload,
                            headers={"X-CSRF-Token": token, "Content-Type": "application/json",
                                     "X-Requested-With": "XMLHttpRequest"})
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:400]}
        item = body.get("d") if isinstance(body, dict) and isinstance(body.get("d"), dict) else body
        return {"ok": True, "dry_run": False, "executed": True, "service": service_key, "entity": entity_name,
                "http_status": resp.status_code, "url": str(resp.request.url), "key": spec["key"],
                "created": item, "location": resp.headers.get("Location")}

    def ping(self, service_key: str = "business_partner") -> dict[str, Any]:
        try:
            page = self.query(service_key, top=1)
            return {"ok": True, "service": service_key, "url": page["url"], "returned": page["returned"],
                    "shape": page["shape"]}
        except SapODataError as exc:
            return {"ok": False, **exc.to_dict()}

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SapODataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
