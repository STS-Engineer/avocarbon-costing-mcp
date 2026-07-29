import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from services.workspace_agent_client import (
    WORKSPACE_AGENT_API_BASE_URL,
    clean_agent_id,
)


MINIMAL_DIAGNOSTIC_INPUT = (
    "API trigger diagnostic. Do not analyze a file. Return a short response."
)
REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-correlation-id",
    "cf-ray",
)
AGENT_ID_ENVIRONMENTS = {
    "bom": "CHATGPT_CHOKE_BOM_AGENT_ID",
    "external": "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
    "most": "CHATGPT_MOST_AGENT_ID",
}


def _trigger_base_url() -> str:
    return str(
        os.getenv("CHATGPT_WORKSPACE_AGENT_TRIGGER_BASE_URL")
        or WORKSPACE_AGENT_API_BASE_URL
    ).strip().rstrip("/")


def _safe_headers(headers: Any) -> Dict[str, str]:
    if not headers:
        return {}
    result = {}
    for name in REQUEST_ID_HEADERS:
        value = headers.get(name)
        if value:
            result[name] = str(value)
    return result


def _retry_after(headers: Any) -> Optional[str]:
    if not headers:
        return None
    value = headers.get("Retry-After")
    return str(value) if value not in [None, ""] else None


def classify_trigger_failure(
    http_status: Optional[int],
    error_type: Optional[str] = None,
    response_body: str = "",
) -> str:
    body = str(response_body or "").lower()
    if http_status == 202:
        return "accepted"
    if http_status == 401:
        return "authentication"
    if http_status == 403:
        return "permission"
    if http_status == 404:
        return "api_channel"
    if http_status == 409:
        if "workspace agent trigger is not currently available" in body:
            return "agent_channel_unavailable"
        if any(term in body for term in ("temporar", "currently unavailable", "try again")):
            return "temporary service failure"
        return "api_channel"
    if http_status == 429:
        return "quota/credits" if any(
            term in body for term in ("quota", "credit", "billing")
        ) else "temporary service failure"
    if http_status in {500, 502, 503, 504}:
        return "temporary service failure"
    if http_status is not None and 400 <= http_status < 500:
        return "invalid endpoint/payload"
    if error_type in {"timeout", "connection_error"}:
        return "temporary service failure"
    return "invalid endpoint/payload"


def run_raw_workspace_trigger(
    *,
    input_text: str,
    conversation_key: Optional[str] = None,
    timeout_seconds: float = 30,
    agent_type: str = "bom",
) -> Dict[str, Any]:
    normalized_agent_type = str(agent_type or "").strip().lower()
    agent_env = AGENT_ID_ENVIRONMENTS.get(normalized_agent_type)
    if not agent_env:
        raise ValueError(
            "agent_type must be one of: "
            + ", ".join(sorted(AGENT_ID_ENVIRONMENTS))
        )
    agent_id = clean_agent_id(os.getenv(agent_env))
    token = str(os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN") or "").strip()
    base_url = _trigger_base_url()
    endpoint = f"{base_url}/{agent_id}/trigger"
    checked_at = datetime.now(timezone.utc).isoformat()

    if not agent_id.startswith("agtch_") or not token:
        missing = []
        if not agent_id.startswith("agtch_"):
            missing.append(agent_env)
        if not token:
            missing.append("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")
        return {
            "configured": False,
            "agent_type": normalized_agent_type,
            "http_status": None,
            "response_headers": {},
            "response_body": "",
            "response_body_length": 0,
            "retry_after": None,
            "elapsed_seconds": 0.0,
            "classification": "authentication" if not token else "api_channel",
            "error_type": "configuration_error",
            "missing_configuration": missing,
            "checked_at": checked_at,
            "agent_id_suffix": agent_id[-8:] if agent_id else None,
            "endpoint_host": urlsplit(endpoint).hostname,
            "request_body_fields": ["input"] + (
                ["conversation_key"] if conversation_key else []
            ),
        }

    body = {"input": str(input_text)}
    if conversation_key is not None:
        body["conversation_key"] = str(conversation_key)
    request_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=request_data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    started = time.perf_counter()
    response_headers = {}
    response_body = ""
    http_status = None
    error_type = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            http_status = response.getcode()
            response_headers = response.headers
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        response_headers = exc.headers
        response_body = exc.read().decode("utf-8", errors="replace")
        error_type = "http_error"
    except (TimeoutError, socket.timeout) as exc:
        response_body = str(exc)
        error_type = "timeout"
    except (urllib.error.URLError, ConnectionError) as exc:
        response_body = str(getattr(exc, "reason", exc))
        error_type = "connection_error"

    elapsed = round(time.perf_counter() - started, 3)
    return {
        "configured": True,
        "agent_type": normalized_agent_type,
        "http_status": http_status,
        "response_headers": _safe_headers(response_headers),
        "response_body": response_body,
        "response_body_length": len(response_body.encode("utf-8")),
        "retry_after": _retry_after(response_headers),
        "elapsed_seconds": elapsed,
        "classification": classify_trigger_failure(
            http_status,
            error_type,
            response_body,
        ),
        "error_type": error_type,
        "checked_at": checked_at,
        "agent_id_suffix": agent_id[-8:],
        "endpoint_host": urlsplit(endpoint).hostname,
        "request_body_fields": sorted(body),
        "request_body_size": len(request_data),
    }


def safe_trigger_error(result: Dict[str, Any]) -> Optional[str]:
    body = result.get("response_body")
    if not body:
        return None
    parsed: Any = body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return body[:500]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("code")
            return str(value)[:500] if value else None
        if isinstance(error, str):
            return error[:500]
        value = parsed.get("message") or parsed.get("detail")
        return str(value)[:500] if value else None
    return str(parsed)[:500]


def run_minimal_trigger_diagnostic() -> Dict[str, Any]:
    result = run_raw_workspace_trigger(input_text=MINIMAL_DIAGNOSTIC_INPUT)
    return {
        "configured": result["configured"],
        "minimal_trigger_http_status": result["http_status"],
        "request_id": next(
            iter((result.get("response_headers") or {}).values()),
            None,
        ),
        "retry_after": result["retry_after"],
        "agent_id_suffix": result["agent_id_suffix"],
        "endpoint_host": result["endpoint_host"],
        "classification": result["classification"],
        "checked_at": result["checked_at"],
    }


def unique_conversation_key() -> str:
    return f"avocarbon-workspace-trigger-diagnostic-{uuid.uuid4()}"
