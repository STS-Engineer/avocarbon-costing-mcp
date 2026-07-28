import json
import hashlib
import logging
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


WORKSPACE_AGENT_API_BASE_URL = "https://api.chatgpt.com/v1/workspace_agents"
WORKSPACE_AGENT_TRIGGER_BODY_FIELDS = frozenset({"input", "conversation_key"})
logger = logging.getLogger(__name__)
FRESH_CONVERSATION_MODE = "new"
CONTINUATION_FIELDS = {
    "conversation_id",
    "conversation",
    "thread_id",
    "thread",
    "session_id",
    "chat_id",
    "previous_response_id",
    "run_context_id",
    "parent_conversation",
    "continuation",
}


def clean_agent_id(agent_id):
    cleaned_id = str(agent_id or "").strip()
    cleaned_id = cleaned_id.rstrip("/")
    if cleaned_id.endswith("/trigger"):
        cleaned_id = cleaned_id[: -len("/trigger")]
    if "/" in cleaned_id:
        cleaned_id = cleaned_id.rsplit("/", 1)[-1]
    return cleaned_id


def _trigger_base_url():
    configured = (
        os.getenv("CHATGPT_WORKSPACE_AGENT_TRIGGER_BASE_URL")
        or os.getenv("WORKSPACE_AGENT_TRIGGER_BASE_URL")
        or WORKSPACE_AGENT_API_BASE_URL
    )
    base_url = str(configured).strip().rstrip("/")
    if base_url.endswith("/mcp") or "mcp-costing.azurewebsites.net" in base_url.lower():
        raise ValueError("Workspace Agent trigger URL must not use the Azure MCP endpoint.")
    return base_url


def _safe_agent_id_prefix(agent_id):
    cleaned = clean_agent_id(agent_id)
    return f"{cleaned[:10]}..." if len(cleaned) > 10 else cleaned


def _response_payload(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _correlation_id(headers):
    if not headers:
        return None
    for name in ["x-request-id", "request-id", "x-correlation-id", "cf-ray"]:
        value = headers.get(name)
        if value:
            return value
    return None


def _retry_after_seconds(headers):
    if not headers:
        return None
    value = str(headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def _safe_identifier(value):
    text = str(value or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return {
        "sha256_prefix": digest,
        "safe_suffix": text[-8:],
    }


def _returned_conversation_identifier(response_payload):
    if not isinstance(response_payload, dict):
        return None
    candidates = [
        response_payload.get("conversation_id"),
        response_payload.get("conversation"),
        (response_payload.get("data") or {}).get("conversation_id")
        if isinstance(response_payload.get("data"), dict)
        else None,
    ]
    return next((value for value in candidates if isinstance(value, str) and value), None)


def _fresh_invocation_values(conversation_key, idempotency_key):
    invocation_id = str(uuid.uuid4())
    base_conversation_key = str(conversation_key or "workspace-agent").strip()
    base_idempotency_key = str(idempotency_key or base_conversation_key).strip()
    return {
        "invocation_id": invocation_id,
        "conversation_key": (
            f"{base_conversation_key}:invocation:{invocation_id}"
        ),
        "idempotency_key": (
            f"{base_idempotency_key}:invocation:{invocation_id}"
        ),
    }


def workspace_agent_configuration(
    agent_id=None,
    access_token=None,
    timeout_seconds=None,
):
    """Return a secret-safe trigger configuration diagnostic."""
    cleaned_agent_id = clean_agent_id(
        agent_id
        if agent_id is not None
        else os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID")
    )
    token = str(
        access_token
        if access_token is not None
        else os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN") or ""
    ).strip()
    try:
        endpoint = f"{_trigger_base_url()}/{{agent_id}}/trigger"
        endpoint_error = None
    except ValueError as exc:
        endpoint = None
        endpoint_error = str(exc)
    try:
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("WORKSPACE_AGENT_TRIGGER_TIMEOUT_SECONDS", "60")
        )
    except (TypeError, ValueError):
        timeout = 60.0
    missing = []
    if not cleaned_agent_id:
        missing.append("CHATGPT_CHOKE_BOM_AGENT_ID")
    elif not cleaned_agent_id.startswith("agtch_"):
        missing.append("CHATGPT_CHOKE_BOM_AGENT_ID(valid agtch_ trigger ID)")
    if not token:
        missing.append("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")
    if endpoint_error:
        missing.append("CHATGPT_WORKSPACE_AGENT_TRIGGER_BASE_URL")
    return {
        "status": "configured" if not missing else "misconfigured",
        "agent_id_present": bool(cleaned_agent_id),
        "agent_id_valid": cleaned_agent_id.startswith("agtch_"),
        "agent_id_masked": (
            f"{cleaned_agent_id[:10]}...{cleaned_agent_id[-4:]}"
            if len(cleaned_agent_id) > 14 else cleaned_agent_id or None
        ),
        "token_present": bool(token),
        "token_length": len(token),
        "endpoint": endpoint,
        "endpoint_error": endpoint_error,
        "invocation_timeout_seconds": timeout,
        "missing_configuration": missing,
        "connectivity_checked": False,
        "note": (
            "Configuration-only health check; no Workspace Agent run was created."
        ),
        "trigger_request_contract": {
            "supported_body_fields": sorted(WORKSPACE_AGENT_TRIGGER_BODY_FIELDS),
            "file_attachments_supported": False,
            "drawing_delivery_mode": "signed_url",
        },
    }


def trigger_workspace_agent(
    agent_id,
    access_token,
    input_text,
    conversation_key=None,
    idempotency_key=None,
    dry_run=True,
    timeout_seconds=None,
    conversation_mode=FRESH_CONVERSATION_MODE,
):
    if conversation_mode != FRESH_CONVERSATION_MODE:
        raise ValueError("Only conversation_mode='new' is supported.")
    invocation = _fresh_invocation_values(conversation_key, idempotency_key)
    conversation_key = invocation["conversation_key"]
    idempotency_key = invocation["idempotency_key"]
    invocation_id = invocation["invocation_id"]
    created_at = datetime.now(timezone.utc).isoformat()
    cleaned_agent_id = clean_agent_id(agent_id)
    access_token = str(
        access_token
        or os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")
        or os.getenv("WORKSPACE_AGENT_ACCESS_TOKEN")
        or ""
    ).strip()
    try:
        base_url = _trigger_base_url()
    except ValueError as exc:
        return {
            "status": "blocked",
            "error_type": "invalid_trigger_url",
            "message": str(exc),
            "endpoint": None,
            "agent_id_prefix": _safe_agent_id_prefix(cleaned_agent_id),
            "token_present": bool(access_token),
        }
    endpoint = f"{base_url}/{cleaned_agent_id}/trigger"
    safe_endpoint = f"{base_url}/{{agent_id}}/trigger"

    if dry_run:
        return {
            "status": "dry_run",
            "agent_id": cleaned_agent_id,
            "conversation_key": conversation_key,
            "idempotency_key": idempotency_key,
            "conversation_mode": conversation_mode,
            "invocation_id": invocation_id,
            "created_at": created_at,
            "input_text": input_text,
            "endpoint": safe_endpoint,
            "agent_id_prefix": _safe_agent_id_prefix(cleaned_agent_id),
            "token_present": bool(access_token),
        }

    missing_inputs = []
    if not cleaned_agent_id:
        missing_inputs.append("agent_id")
    elif not cleaned_agent_id.startswith("agtch_"):
        missing_inputs.append("valid agtch_ agent_id")
    if not access_token:
        missing_inputs.append("access_token")
    if not input_text:
        missing_inputs.append("input_text")

    if missing_inputs:
        return {
            "status": "blocked",
            "missing_inputs": missing_inputs,
            "message": "Workspace Agent trigger cannot run without required inputs.",
            "endpoint": safe_endpoint,
            "method": "POST",
            "agent_id_prefix": _safe_agent_id_prefix(cleaned_agent_id),
            "token_present": bool(access_token),
            "payload_size": 0,
        }

    # The trigger API uses a unique conversation_key to request an independent
    # conversation. Continuation identifiers are deliberately never accepted
    # or copied into this freshly constructed request object.
    body = {"input": str(input_text).strip()}
    if conversation_key:
        body["conversation_key"] = str(conversation_key).strip()
    assert not CONTINUATION_FIELDS.intersection(body)
    request_data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    request = urllib.request.Request(
        endpoint.strip(),
        data=request_data,
        headers=headers,
        method="POST",
    )
    try:
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("WORKSPACE_AGENT_TRIGGER_TIMEOUT_SECONDS", "60")
        )
    except (TypeError, ValueError):
        timeout = 60.0

    diagnostic = {
        "endpoint": safe_endpoint,
        "method": "POST",
        "agent_id_prefix": _safe_agent_id_prefix(cleaned_agent_id),
        "token_present": bool(access_token),
        "payload_size": len(request_data),
        "conversation_mode": conversation_mode,
        "invocation_id": invocation_id,
        "conversation_key_audit": _safe_identifier(conversation_key),
        "created_at": created_at,
    }
    logger.info("Workspace Agent trigger request: %s", json.dumps(diagnostic))
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            response_text = response.read().decode("utf-8", errors="replace")
            result = {
                "status": "accepted" if status_code == 202 else "failed",
                "http_status": status_code,
                "note": (
                    "Agent request accepted and queued. Waiting for callback."
                    if status_code == 202
                    else "Workspace Agent trigger request was not accepted."
                ),
                "response": _response_payload(response_text),
                "request_correlation_id": _correlation_id(response.headers),
                "retry_after_seconds": _retry_after_seconds(response.headers),
                "conversation_url_verified": False,
            }
    except urllib.error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        result = {
            "status": "failed",
            "http_status": exc.code,
            "note": "Workspace Agent trigger failed.",
            "error": response_text,
            "response": _response_payload(response_text),
            "error_type": "http_error",
            "request_correlation_id": _correlation_id(exc.headers),
            "retry_after_seconds": _retry_after_seconds(exc.headers),
        }
    except (TimeoutError, socket.timeout) as exc:
        result = {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger timed out.",
            "error": str(exc),
            "error_type": "timeout",
        }
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        error_type = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "connection_error"
        result = {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger connection failed.",
            "error": str(reason),
            "error_type": error_type,
        }
    except ConnectionError as exc:
        result = {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger connection failed.",
            "error": str(exc),
            "error_type": "connection_error",
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger failed.",
            "error": str(exc),
            "error_type": "unexpected_error",
        }
    returned_conversation_id = _returned_conversation_identifier(
        result.get("response")
    )
    result.update(diagnostic)
    result["conversation_key"] = conversation_key
    result["idempotency_key"] = idempotency_key
    result["returned_conversation_id_audit"] = _safe_identifier(
        returned_conversation_id
    )
    result["invocation_audit"] = {
        "invocation_id": invocation_id,
        "conversation_mode": conversation_mode,
        "conversation_key": _safe_identifier(conversation_key),
        "returned_conversation_id": _safe_identifier(returned_conversation_id),
        "created_at": created_at,
    }
    result["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    logger.info(
        "Workspace Agent trigger response: %s",
        json.dumps({
            **diagnostic,
            "elapsed_seconds": result["elapsed_seconds"],
            "http_status": result.get("http_status"),
            "response_type": type(result.get("response")).__name__,
            "response_keys": sorted(result["response"])
            if isinstance(result.get("response"), dict)
            else [],
            "error_type": result.get("error_type"),
            "request_correlation_id": result.get("request_correlation_id"),
        }, default=str),
    )
    return result
