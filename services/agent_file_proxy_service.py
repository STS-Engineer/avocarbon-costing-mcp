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
from urllib.parse import parse_qs, quote, unquote, urlsplit

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


def create_agent_file_token(project_code: str, filename: str, expiry_seconds: int = 3600) -> str:
    project = _safe_part(project_code, "project_code")
    name = _safe_part(filename, "filename")
    current_timestamp = int(time.time())
    expires_at = current_timestamp + max(3600, int(expiry_seconds))
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
    expiry_seconds: int = 3600,
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


def inspect_signed_url_expiry(
    url: str,
    now_timestamp: int | None = None,
) -> Dict[str, Any]:
    current_timestamp = int(time.time()) if now_timestamp is None else int(now_timestamp)
    parsed = urlsplit(str(url or "").strip())
    query = parse_qs(parsed.query)
    expires_at = None
    expiry_source = None

    token = (query.get("token") or [None])[0]
    if token:
        try:
            expires_at = int(str(token).split(".", 1)[0])
            expiry_source = "backend_signed_token"
        except (TypeError, ValueError):
            return {
                "valid": False,
                "reason": "malformed_token",
                "safe_url_path": parsed.path,
                "expires_at": None,
                "remaining_seconds": None,
            }
    else:
        sas_expiry = (query.get("se") or [None])[0]
        if sas_expiry:
            try:
                parsed_expiry = datetime.fromisoformat(
                    unquote(str(sas_expiry)).replace("Z", "+00:00")
                )
                if parsed_expiry.tzinfo is None:
                    parsed_expiry = parsed_expiry.replace(tzinfo=timezone.utc)
                expires_at = int(parsed_expiry.timestamp())
                expiry_source = "azure_sas"
            except (TypeError, ValueError):
                return {
                    "valid": False,
                    "reason": "malformed_sas_expiry",
                    "safe_url_path": parsed.path,
                    "expires_at": None,
                    "remaining_seconds": None,
                }

    remaining_seconds = (
        expires_at - current_timestamp if expires_at is not None else None
    )
    return {
        "valid": expires_at is not None and remaining_seconds >= 1800,
        "reason": (
            "valid"
            if expires_at is not None and remaining_seconds >= 1800
            else "expiry_too_close"
            if expires_at is not None
            else "expiry_not_found"
        ),
        "safe_url_path": parsed.path,
        "expiry_source": expiry_source,
        "expires_at": expires_at,
        "expires_at_utc": (
            datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
            if expires_at is not None
            else None
        ),
        "remaining_seconds": remaining_seconds,
    }


def verify_agent_pdf_url(url: str, timeout_seconds: float = 15.0) -> Dict[str, Any]:
    checked_url = str(url or "").strip()
    parsed = urlsplit(checked_url)
    expiry = inspect_signed_url_expiry(checked_url)
    safe_url_path = parsed.path
    if not expiry.get("valid"):
        reason = expiry.get("reason")
        error_code = (
            "drawing_url_expired"
            if reason in {"expiry_too_close", "malformed_token", "malformed_sas_expiry"}
            else "drawing_url_expiry_unknown"
        )
        result = {
            "success": False,
            "method": "GET",
            "safe_url_path": safe_url_path,
            "error_code": error_code,
            "rejection_reason": reason,
            "token_expiry_time": expiry.get("expires_at_utc"),
            "remaining_token_lifetime_seconds": expiry.get("remaining_seconds"),
        }
        logger.warning("Agent PDF preflight rejected: %s", json.dumps(result))
        return result
    request = urllib.request.Request(
        checked_url,
        headers={
            "Accept": "application/pdf",
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
            redirected = response.geturl() != checked_url
            valid = (
                response.status == 200
                and "application/pdf" in content_type
                and content_length > 0
                and first_bytes.startswith(b"%PDF")
                and not redirected
            )
            if redirected:
                error_code = "drawing_url_forbidden"
            elif content_length <= 0 or not first_bytes:
                error_code = "drawing_empty"
            elif "application/pdf" not in content_type or not first_bytes.startswith(b"%PDF"):
                error_code = "drawing_not_pdf"
            elif response.status != 200:
                error_code = "drawing_url_forbidden"
            else:
                error_code = None
            result = {
                "success": valid,
                "method": "GET",
                "http_status": response.status,
                "content_type": content_type,
                "content_length": content_length,
                "pdf_signature_present": first_bytes.startswith(b"%PDF"),
                "safe_url_path": safe_url_path,
                "redirected": redirected,
                "error_code": error_code,
                "token_expiry_time": expiry.get("expires_at_utc"),
                "remaining_token_lifetime_seconds": expiry.get("remaining_seconds"),
                "rejection_reason": None if valid else "invalid_pdf_response",
            }
            logger.info("Agent PDF preflight: %s", json.dumps(result))
            return result
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
        error_code = (
            "drawing_url_forbidden"
            if exc.code in {401, 403}
            else "drawing_url_expired"
            if exc.code in {408, 410}
            else "drawing_not_pdf"
        )
        result = {
            "success": False,
            "method": "GET",
            "http_status": exc.code,
            "safe_url_path": safe_url_path,
            "error_code": error_code,
            "rejection_reason": rejection_reason,
            "token_expiry_time": expiry.get("expires_at_utc"),
            "remaining_token_lifetime_seconds": expiry.get("remaining_seconds"),
        }
        logger.warning("Agent PDF preflight HTTP failure: %s", json.dumps(result))
        return result
    except (OSError, urllib.error.URLError, ValueError) as exc:
        result = {
            "success": False,
            "method": "GET",
            "safe_url_path": safe_url_path,
            "error_code": "drawing_url_forbidden",
            "rejection_reason": type(exc).__name__,
            "token_expiry_time": expiry.get("expires_at_utc"),
            "remaining_token_lifetime_seconds": expiry.get("remaining_seconds"),
        }
        logger.warning("Agent PDF preflight connection failure: %s", json.dumps(result))
        return result
