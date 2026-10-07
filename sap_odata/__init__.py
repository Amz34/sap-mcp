"""SAP OData connector package (read-first, governance-gated writes)."""
from .client import SapODataClient, SapODataError, SapWriteBlocked
from .config import SERVICES, SANDBOX_ODATA_PATH, ONPREM_ODATA_PATH, SapConfig, load_config

__all__ = [
    "SapODataClient",
    "SapODataError",
    "SapWriteBlocked",
    "SapConfig",
    "load_config",
    "SERVICES",
    "SANDBOX_ODATA_PATH",
    "ONPREM_ODATA_PATH",
]
__version__ = "0.1.0"
