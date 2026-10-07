"""Configuration layer for the SAP OData connector.

Env-first and secret-safe: nothing here ever prints a secret unless redacted() is
explicitly asked, and writes are OFF unless a human flips SAP_WRITE_ENABLED=1.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# SAP Business Accelerator Hub sandbox layout (read-only demo APIs, free API key)
SANDBOX_ODATA_PATH = "/s4hanacloud/sap/opu/odata/sap"
# Classic on-premise S/4HANA / ECC (NetWeaver Gateway) layout
ONPREM_ODATA_PATH = "/sap/opu/odata/sap"
# S/4HANA OData V4 layout (public cloud APIs)
V4_ODATA_PATH = "/sap/opu/odata4/sap"

CONFIG_FILE = Path(os.environ.get("SAP_CONFIG_FILE", "~/.config/sap-connector/config.json")).expanduser()

# Whitelisted read surfaces. Adding a service here is the only way a generic
# query tool can reach SAP -> keeps the agent surface narrow (least privilege).
SERVICES: dict[str, dict[str, Any]] = {
    "business_partner": {
        "service": "API_BUSINESS_PARTNER",
        "entity": "A_BusinessPartner",
        "key": "BusinessPartner",
        "label": "Business partner master data (customers, suppliers, contacts)",
        "fields": ["BusinessPartner", "BusinessPartnerCategory", "BusinessPartnerFullName",
                   "BusinessPartnerName", "OrganizationBPName1", "SearchTerm1", "Country",
                   "Region", "CreationDate", "IsBlocked"],
    },
    "sales_order": {
        "service": "API_SALES_ORDER_SRV",
        "entity": "A_SalesOrder",
        "key": "SalesOrder",
        "label": "Sales orders (header level)",
        "fields": ["SalesOrder", "SalesOrderType", "SoldToParty", "CustomerReference",
                   "CreationDate", "TotalNetAmount", "OverallSDProcessStatus",
                   "SalesOrganization", "TransactionCurrency"],
    },
    "purchase_order": {
        "service": "API_PURCHASEORDER_PROCESS_SRV",
        "entity": "A_PurchaseOrder",
        "key": "PurchaseOrder",
        "label": "Purchase orders (header level)",
        "fields": ["PurchaseOrder", "Supplier", "CompanyCode", "PurchaseOrderDate",
                   "PurchasingGroup", "PurchasingDocumentCategory", "DocumentCurrency",
                   "PurchaseOrderNetAmount", "PurchasingProcessingStatus"],
    },
    "product": {
        "service": "API_PRODUCT_SRV",
        "entity": "A_Product",
        "key": "Product",
        "label": "Products / materials (header level)",
        "fields": ["Product", "ProductType", "BaseUnit", "ProductGroup", "GrossWeight",
                   "NetWeight", "CreationDate"],
    },
    "supplier": {
        "service": "API_BUSINESS_PARTNER",
        "entity": "A_Supplier",
        "key": "Supplier",
        "label": "Supplier master data",
        "fields": ["Supplier", "SupplierName", "Country", "CreationDate", "IsBlocked"],
    },
}


def _env(env: dict[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    val = env.get(key)
    if val is None or val == "":
        return default
    return val


def _as_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class SapConfig:
    """Connection + governance settings for one SAP system / one client."""

    label: str = "default"
    base_url: str = ""
    odata_path: str = SANDBOX_ODATA_PATH
    api_key: Optional[str] = None          # SAP Business Accelerator Hub / API Mgmt key -> `apikey` header
    auth_type: str = "none"               # none | basic | oauth2
    username: Optional[str] = None
    password: Optional[str] = None
    oauth_token_url: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    sap_client: Optional[str] = None       # sap-client header for on-prem systems (e.g. "100")
    odata_version: str = "auto"            # auto | v2 | v4
    timeout: float = 30.0
    max_retries: int = 3
    page_size: int = 100
    max_pages: int = 10
    verify_tls: bool = True
    write_enabled: bool = False            # governance gate: agent writes blocked unless True
    extra_headers: dict[str, str] = field(default_factory=dict)

    # ---------- constructors ----------
    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None, **overrides: Any) -> "SapConfig":
        env = dict(os.environ if env is None else env)
        cfg = cls(
            label=_env(env, "SAP_LABEL", "default") or "default",
            base_url=_env(env, "SAP_BASE_URL", "") or "",
            odata_path=_env(env, "SAP_ODATA_PATH", SANDBOX_ODATA_PATH) or SANDBOX_ODATA_PATH,
            api_key=_env(env, "SAP_APIKEY") or _env(env, "SAP_API_KEY"),
            auth_type=(_env(env, "SAP_AUTH_TYPE", "none") or "none").lower(),
            username=_env(env, "SAP_USERNAME"),
            password=_env(env, "SAP_PASSWORD"),
            oauth_token_url=_env(env, "SAP_OAUTH_TOKEN_URL"),
            oauth_client_id=_env(env, "SAP_OAUTH_CLIENT_ID"),
            oauth_client_secret=_env(env, "SAP_OAUTH_CLIENT_SECRET"),
            sap_client=_env(env, "SAP_CLIENT"),
            odata_version=(_env(env, "SAP_ODATA_VERSION", "auto") or "auto").lower(),
            timeout=float(_env(env, "SAP_TIMEOUT", "30") or 30),
            max_retries=int(_env(env, "SAP_MAX_RETRIES", "3") or 3),
            page_size=int(_env(env, "SAP_PAGE_SIZE", "100") or 100),
            max_pages=int(_env(env, "SAP_MAX_PAGES", "10") or 10),
            verify_tls=_as_bool(_env(env, "SAP_VERIFY_TLS"), True),
            write_enabled=_as_bool(_env(env, "SAP_WRITE_ENABLED"), False),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        if cfg.auth_type == "none" and cfg.username and cfg.password:
            cfg.auth_type = "basic"
        return cfg

    @classmethod
    def from_file(cls, path: Optional[Path | str] = None, profile: Optional[str] = None) -> "SapConfig":
        path = Path(path).expanduser() if path else CONFIG_FILE
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text() or "{}")
        profiles = data.get("profiles") or {}
        active = profile or data.get("active")
        raw: dict[str, Any] = dict(profiles.get(active, {})) if active else {k: v for k, v in data.items() if k != "profiles"}
        cfg = cls.from_env(env={})
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    # ---------- helpers ----------
    def service_base(self, service: str) -> str:
        base = (self.base_url or "").rstrip("/")
        path = "/" + (self.odata_path or SANDBOX_ODATA_PATH).strip("/")
        return f"{base}{path}/{service}"

    def auth_summary(self) -> dict[str, Any]:
        return {"label": self.label, "base_url": self.base_url or None, "api_key": "set" if self.api_key else None,
                "auth_type": self.auth_type, "sap_client": self.sap_client, "write_enabled": self.write_enabled,
                "source": "env+file"}

    def redacted(self) -> dict[str, Any]:
        def mask(v: Optional[str]) -> Optional[str]:
            if not v:
                return None
            return f"***{v[-4:]}" if len(v) > 4 else "***"

        return {
            "label": self.label, "base_url": self.base_url, "odata_path": self.odata_path,
            "api_key": mask(self.api_key), "auth_type": self.auth_type, "username": self.username,
            "password": mask(self.password), "oauth_client_id": self.oauth_client_id,
            "oauth_client_secret": mask(self.oauth_client_secret), "sap_client": self.sap_client,
            "odata_version": self.odata_version, "timeout": self.timeout, "max_retries": self.max_retries,
            "page_size": self.page_size, "max_pages": self.max_pages, "verify_tls": self.verify_tls,
            "write_enabled": self.write_enabled,
        }

    def is_configured(self) -> bool:
        return bool(self.base_url)


def load_config(profile: Optional[str] = None, path: Optional[Path | str] = None, **overrides: Any) -> SapConfig:
    """File -> env precedence, so a client profile can live on disk and secrets in env."""
    cfg = SapConfig.from_file(path=path, profile=profile)
    env_cfg = SapConfig.from_env()
    for key in ("base_url", "api_key", "auth_type", "username", "password", "oauth_token_url",
                "oauth_client_id", "oauth_client_secret", "sap_client", "odata_version", "verify_tls"):
        if getattr(env_cfg, key) not in (None, "", "none", "auto") or key in ("verify_tls", "write_enabled"):
            setattr(cfg, key, getattr(env_cfg, key))
    if os.environ.get("SAP_WRITE_ENABLED"):
        cfg.write_enabled = env_cfg.write_enabled
    if os.environ.get("SAP_LABEL"):
        cfg.label = env_cfg.label
    for key, value in overrides.items():
        if value is not None and hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
