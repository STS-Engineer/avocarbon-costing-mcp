import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request


WORKSPACE_AGENT_API_BASE_URL = "https://api.chatgpt.com/v1/workspace_agents"
logger = logging.getLogger(__name__)


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


def trigger_workspace_agent(
    agent_id,
    access_token,
    input_text,
    conversation_key=None,
    idempotency_key=None,
    dry_run=True,
    timeout_seconds=None,
):
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

    body = {"input": str(input_text).strip()}
    if conversation_key:
        body["conversation_key"] = str(conversation_key).strip()
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
    result.update(diagnostic)
    result["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    logger.info(
        "Workspace Agent trigger response: %s",
        json.dumps({
            **diagnostic,
            "elapsed_seconds": result["elapsed_seconds"],
            "http_status": result.get("http_status"),
            "response": result.get("response") or result.get("error"),
            "request_correlation_id": result.get("request_correlation_id"),
        }, default=str),
    )
    return result
