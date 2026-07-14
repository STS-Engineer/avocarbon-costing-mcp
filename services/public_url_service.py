import os
from typing import Any, Dict, Optional


def normalize_public_rest_base_url(value: Optional[str]) -> str:
    base_url = str(value or "").strip().rstrip("/")
    if base_url.lower().endswith("/mcp"):
        base_url = base_url[:-4].rstrip("/")
    return base_url


def get_public_rest_base_url(fallback_url: Optional[str] = None) -> str:
    configured = os.getenv("PUBLIC_BASE_URL")
    return normalize_public_rest_base_url(
        configured or fallback_url or "http://127.0.0.1:8000"
    )


def get_public_url_diagnostics() -> Dict[str, Any]:
    raw = str(os.getenv("PUBLIC_BASE_URL") or "").strip() or None
    resolved = get_public_rest_base_url()
    return {
        "public_base_url_raw": raw,
        "public_rest_base_url_resolved": resolved,
        "mcp_url": f"{resolved}/mcp",
    }
