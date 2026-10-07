"""Configuration for the SAP Business One Service Layer connector.

Env-first and secret-safe, same conventions as the OData connector:
  * every value can come from the environment or from a profile in
    ~/.config/sap-b1/config.json (profile name picked by SAP_B1_PROFILE)
  * writes are OFF unless a human sets SAP_B1_WRITE_ENABLED=1
  * nothing here ever logs a password: use redacted()
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

DEFAULT_SERVICE_LAYER_PATH = "/b1s/v1"
DEFAULT_CONFIG_FILE = Path(
    os.environ.get("SAP_B1_CONFIG", "~/.config/sap-b1/config.json")
).expanduser()

# Read-first entity map. "path" is the Service Layer collection, "key" its primary key.
ENTITIES: dict[str, dict[str, Any]] = {
    "business_partner": {
        "path": "BusinessPartners",
        "key": "CardCode",
        "label": "Business Partner",
        "fields": ["CardCode", "CardName", "CardType", "GroupCode", "Phone1",
                   "EmailAddress", "City", "Country", "Currency", "Balance", "Valid"],
    },
    "item": {
        "path": "Items",
        "key": "ItemCode",
        "label": "Item",
        "fields": ["ItemCode", "ItemName", "ItemType", "ItemsGroupCode",
                   "QuantityOnStock", "SalesUnit", "PurchaseUnit", "Valid"],
    },
    "order": {
        "path": "Orders",
        "key": "DocEntry",
        "label": "Sales Order",
        "fields": ["DocEntry", "DocNum", "CardCode", "CardName", "DocDate",
                   "DocDueDate", "DocTotal", "DocumentStatus", "SalesPersonCode"],
    },
    "invoice": {
        "path": "Invoices",
        "key": "DocEntry",
        "label": "A/R Invoice",
        "fields": ["DocEntry", "DocNum", "CardCode", "CardName", "DocDate",
                   "DocTotal", "DocumentStatus", "PaidToDate"],
    },
    "purchase_order": {
        "path": "PurchaseOrders",
        "key": "DocEntry",
        "label": "Purchase Order",
        "fields": ["DocEntry", "DocNum", "CardCode", "CardName", "DocDate",
                   "DocDueDate", "DocTotal", "DocumentStatus"],
    },
}

# Writable entities (create/PATCH) - intentionally a short whitelist.
WRITABLE_ENTITIES: dict[str, dict[str, Any]] = {
    "business_partner": {
        "path": "BusinessPartners",
        "key": "CardCode",
        "label": "Business Partner",
        "required": ["CardCode", "CardName", "CardType"],
        "allowed": ["CardCode", "CardName", "CardType", "GroupCode", "Phone1",
                    "EmailAddress", "City", "Country", "Currency", "Notes",
                    "ContactPerson", "Cellular"],
    },
    "item": {
        "path": "Items",
        "key": "ItemCode",
        "label": "Item",
        "required": ["ItemCode", "ItemName", "ItemType"],
        "allowed": ["ItemCode", "ItemName", "ItemType", "ItemsGroupCode",
                    "SalesUnit", "PurchaseUnit", "SalesItem", "PurchaseItem", "Valid"],
    },
}

_TRUTHY = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class B1Config:
    """One SAP Business One company connection."""

    label: str = "b1"
    base_url: str = ""              # e.g. https://sap-b1.example.com:50000
    service_layer_path: str = DEFAULT_SERVICE_LAYER_PATH
    company_db: str = ""
    username: str = ""
    password: str = ""
    language: str = "en-us"
    verify_tls: bool = True
    timeout: float = 30.0
    write_enabled: bool = False
    session_ttl: int = 25 * 60       # seconds; Service Layer default is 30 min idle
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- derived -----------------------------------------------------------
    @property
    def session_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.service_layer_path}"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.company_db and self.username and self.password)

    def missing(self) -> list[str]:
        need = {"SAP_B1_BASE_URL": self.base_url, "SAP_B1_COMPANY_DB": self.company_db,
                "SAP_B1_USERNAME": self.username, "SAP_B1_PASSWORD": self.password}
        return [k for k, v in need.items() if not v]

    def entity(self, name: str, *, write: bool = False) -> dict[str, Any]:
        table = WRITABLE_ENTITIES if write else ENTITIES
        try:
            return table[name]
        except KeyError:
            raise KeyError(
                f"unknown entity {name!r}; known: {sorted(table)}"
            ) from None

    def redacted(self) -> dict[str, Any]:
        """Safe to print / return to an agent: never includes the password."""
        return {
            "label": self.label,
            "base_url": self.base_url,
            "service_layer_path": self.service_layer_path,
            "company_db": self.company_db,
            "username": self.username,
            "password_set": bool(self.password),
            "language": self.language,
            "verify_tls": self.verify_tls,
            "write_enabled": self.write_enabled,
            "configured": self.configured,
            "missing_env": self.missing(),
        }


_ENV_MAP = {
    "label": "SAP_B1_LABEL",
    "base_url": "SAP_B1_BASE_URL",
    "service_layer_path": "SAP_B1_PATH",
    "company_db": "SAP_B1_COMPANY_DB",
    "username": "SAP_B1_USERNAME",
    "password": "SAP_B1_PASSWORD",
    "language": "SAP_B1_LANGUAGE",
}


def _from_env() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field_name, env_name in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw:
            values[field_name] = raw
    if os.environ.get("SAP_B1_VERIFY_TLS") is not None:
        values["verify_tls"] = _env_bool("SAP_B1_VERIFY_TLS", True)
    if os.environ.get("SAP_B1_INSECURE") is not None:
        values["verify_tls"] = not _env_bool("SAP_B1_INSECURE", False)
    if os.environ.get("SAP_B1_WRITE_ENABLED") is not None:
        values["write_enabled"] = _env_bool("SAP_B1_WRITE_ENABLED", False)
    if os.environ.get("SAP_B1_TIMEOUT") is not None:
        try:
            values["timeout"] = float(os.environ["SAP_B1_TIMEOUT"])
        except ValueError:
            pass
    return values


def _from_profile(profile: Optional[str]) -> dict[str, Any]:
    if not DEFAULT_CONFIG_FILE.exists():
        return {}
    try:
        blob = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if profile:
        profiles = blob.get("profiles", {})
        if profile not in profiles:
            raise KeyError(
                f"profile {profile!r} not found in {DEFAULT_CONFIG_FILE}; "
                f"available: {sorted(profiles)}"
            )
        return dict(profiles[profile])
    if "profiles" in blob:                       # no profile asked for: use "default"
        profiles = blob.get("profiles", {})
        return dict(profiles.get(blob.get("active", "default"), {}))
    return dict(blob)


def load_config(profile: Optional[str] = None, **overrides: Any) -> B1Config:
    """Precedence: explicit overrides > environment > profile file > defaults."""
    profile = profile or os.environ.get("SAP_B1_PROFILE")
    values: dict[str, Any] = {}
    values.update(_from_profile(profile))
    values.update(_from_env())
    values.update({k: v for k, v in overrides.items() if v is not None})
    known = {f for f in B1Config.__dataclass_fields__}
    unknown = {k: v for k, v in values.items() if k not in known}
    clean = {k: v for k, v in values.items() if k in known}
    cfg = B1Config(**clean)
    if unknown:
        cfg = replace(cfg, extra={**cfg.extra, **unknown})
    return cfg
