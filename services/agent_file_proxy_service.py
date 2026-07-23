import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote, unquote, urlsplit

from services.project_data_paths import CUSTOMER_INPUT_DIR
from services.public_url_service import normalize_public_rest_base_url


logger = logging.getLogger(__name__)


def _safe_part(value: str, field_name: str) -> str:
    text = unquote(str(value or "")).strip()
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


def canonical_agent_file_relative_path(project_code: str, filename: str) -> str:
    project = _safe_part(project_code, "project_code")
    name = _safe_part(filename, "filename")
    return f"uploads/{project}/{name}"


def agent_file_signature_message(project_code: str, filename: str, expires_at: int) -> str:
    return f"{canonical_agent_file_relative_path(project_code, filename)}\n{int(expires_at)}"


def _signature(project_code: str, filename: str, expires_at: int) -> str:
    message = agent_file_signature_message(project_code, filename, expires_at).encode("utf-8")
    return hmac.new(_signing_secret(), message, hashlib.sha256).hexdigest()


def create_agent_file_token(project_code: str, filename: str, expiry_seconds: int = 14400) -> str:
    project = _safe_part(project_code, "project_code")
    name = _safe_part(filename, "filename")
    current_timestamp = int(time.time())
    expires_at = current_timestamp + max(7200, int(expiry_seconds))
    logger.info(
        "Agent PDF token created path=%s expires=%s current=%s",
        canonical_agent_file_relative_path(project, name),
        datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        datetime.fromtimestamp(current_timestamp, timezone.utc).isoformat(),
    )
    return f"{expires_at}.{_signature(project, name, expires_at)}"


def validate_agent_file_token(project_code: str, filename: str, token: str) -> bool:
    return inspect_agent_file_token(project_code, filename, token)["valid"]


def inspect_agent_file_token(
    project_code: str,
    filename: str,
    token: str,
    now_timestamp: int | None = None,
) -> Dict[str, Any]:
    current_timestamp = int(time.time()) if now_timestamp is None else int(now_timestamp)
    try:
        expires_text, supplied_signature = str(token or "").split(".", 1)
        expires_at = int(expires_text)
    except (TypeError, ValueError):
        return {
            "valid": False,
            "reason": "malformed_token",
            "normalized_relative_path": None,
            "expires_at": None,
            "current_utc": datetime.fromtimestamp(current_timestamp, timezone.utc).isoformat(),
        }
    try:
        project = _safe_part(project_code, "project_code")
        name = _safe_part(filename, "filename")
        relative_path = canonical_agent_file_relative_path(project, name)
        signature_message = agent_file_signature_message(project, name, expires_at)
        expected = _signature(project, name, expires_at)
    except (RuntimeError, ValueError) as exc:
        return {
            "valid": False,
            "reason": "token_configuration_error",
            "error": str(exc),
            "normalized_relative_path": None,
            "expires_at": expires_at,
            "current_utc": datetime.fromtimestamp(current_timestamp, timezone.utc).isoformat(),
        }
    if expires_at < current_timestamp:
        reason = "expired"
        valid = False
    elif not hmac.compare_digest(supplied_signature, expected):
        reason = "signature_mismatch"
        valid = False
    else:
        reason = "valid"
        valid = True
    return {
        "valid": valid,
        "reason": reason,
        "normalized_relative_path": relative_path,
        "signature_message": signature_message,
        "expires_at": expires_at,
        "expires_at_utc": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        "current_timestamp": current_timestamp,
        "current_utc": datetime.fromtimestamp(current_timestamp, timezone.utc).isoformat(),
        "signature_prefix": supplied_signature[:8],
    }


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
    checked_url = str(url or "").strip()
    parsed = urlsplit(checked_url)
    request = urllib.request.Request(
        checked_url,
        headers={
            "Accept": "application/pdf",
            "Range": "bytes=0-4095",
            "User-Agent": "AVOCarbon-Costing-Backend/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            content_length_header = response.headers.get("Content-Length")
            first_bytes = response.read(4096)
            content_length = int(content_length_header) if content_length_header else len(first_bytes)
            valid = (
                response.status in {200, 206}
                and content_type == "application/pdf"
                and content_length > 0
                and first_bytes.startswith(b"%PDF")
            )
            return {
                "success": valid,
                "method": "GET",
                "http_status": response.status,
                "content_type": content_type,
                "content_length": content_length,
                "pdf_signature_present": first_bytes.startswith(b"%PDF"),
                "final_url_host": urlsplit(response.geturl()).netloc,
                "requested_url_host": parsed.netloc,
                "redirected": response.geturl() != checked_url,
                "rejection_reason": None if valid else "invalid_pdf_response",
            }
    except urllib.error.HTTPError as exc:
        response_body = ""
        try:
            response_body = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            pass
        rejection_reason = f"http_{exc.code}"
        try:
            detail = json.loads(response_body).get("detail")
            if detail:
                rejection_reason = str(detail)
        except (AttributeError, json.JSONDecodeError):
            pass
        return {
            "success": False,
            "method": "GET",
            "http_status": exc.code,
            "error": str(exc),
            "rejection_reason": rejection_reason,
            "requested_url_host": parsed.netloc,
        }
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return {
            "success": False,
            "method": "GET",
            "error": str(exc),
            "rejection_reason": type(exc).__name__,
            "requested_url_host": parsed.netloc,
        }
