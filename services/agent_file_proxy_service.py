import hashlib
import hmac
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote

from services.project_data_paths import CUSTOMER_INPUT_DIR
from services.public_url_service import normalize_public_rest_base_url


def _safe_part(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or text != Path(text).name:
        raise ValueError(f"Invalid {field_name}.")
    return text


def _signing_secret() -> bytes:
    secret = (
        os.getenv("AGENT_FILE_SIGNING_SECRET")
        or os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")
        or ""
    ).strip()
    if not secret:
        raise RuntimeError(
            "AGENT_FILE_SIGNING_SECRET or CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN is required."
        )
    return secret.encode("utf-8")


def uploaded_pdf_path(project_code: str, filename: str) -> Path:
    project = _safe_part(project_code, "project_code")
    name = _safe_part(filename, "filename")
    if Path(name).suffix.lower() != ".pdf":
        raise ValueError("Only PDF files can be served.")
    upload_root = (CUSTOMER_INPUT_DIR / "uploads").resolve()
    candidate = (upload_root / project / name).resolve()
    if upload_root not in candidate.parents:
        raise ValueError("Invalid uploaded PDF path.")
    return candidate


def _signature(project_code: str, filename: str, expires_at: int) -> str:
    message = f"{project_code}\n{filename}\n{expires_at}".encode("utf-8")
    return hmac.new(_signing_secret(), message, hashlib.sha256).hexdigest()


def create_agent_file_token(project_code: str, filename: str, expiry_seconds: int = 14400) -> str:
    project = _safe_part(project_code, "project_code")
    name = _safe_part(filename, "filename")
    expires_at = int(time.time()) + max(7200, int(expiry_seconds))
    return f"{expires_at}.{_signature(project, name, expires_at)}"


def validate_agent_file_token(project_code: str, filename: str, token: str) -> bool:
    try:
        expires_text, supplied_signature = str(token or "").split(".", 1)
        expires_at = int(expires_text)
    except (TypeError, ValueError):
        return False
    if expires_at < int(time.time()):
        return False
    expected = _signature(
        _safe_part(project_code, "project_code"),
        _safe_part(filename, "filename"),
        expires_at,
    )
    return hmac.compare_digest(supplied_signature, expected)


def build_agent_file_url(
    public_base_url: str,
    project_code: str,
    filename: str,
    expiry_seconds: int = 14400,
) -> str:
    project = _safe_part(project_code, "project_code")
    name = _safe_part(filename, "filename")
    token = create_agent_file_token(project, name, expiry_seconds=expiry_seconds)
    base = normalize_public_rest_base_url(public_base_url)
    if not base:
        raise ValueError("PUBLIC_BASE_URL is required to build the Agent PDF proxy URL.")
    return (
        f"{base}/api/choke-costing/agent-files/"
        f"{quote(project, safe='')}/{quote(name, safe='')}?token={quote(token, safe='')}"
    )


def verify_agent_pdf_url(url: str, timeout_seconds: float = 15.0) -> Dict[str, Any]:
    request = urllib.request.Request(
        str(url or "").strip(),
        headers={"Accept": "application/pdf", "User-Agent": "AVOCarbon-Costing-Backend/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            content_length_header = response.headers.get("Content-Length")
            first_bytes = response.read(16)
            content_length = int(content_length_header) if content_length_header else len(first_bytes)
            valid = response.status == 200 and content_type == "application/pdf" and content_length > 0
            return {
                "success": valid,
                "http_status": response.status,
                "content_type": content_type,
                "content_length": content_length,
                "pdf_signature_present": first_bytes.startswith(b"%PDF"),
            }
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
