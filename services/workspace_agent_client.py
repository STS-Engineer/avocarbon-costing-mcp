import json
import os
import socket
import urllib.error
import urllib.request


WORKSPACE_AGENT_API_BASE_URL = "https://api.chatgpt.com/v1/workspace_agents"


def clean_agent_id(agent_id):
    cleaned_id = str(agent_id or "").strip()
    cleaned_id = cleaned_id.rstrip("/")
    if cleaned_id.endswith("/trigger"):
        cleaned_id = cleaned_id[: -len("/trigger")]
    if "/" in cleaned_id:
        cleaned_id = cleaned_id.rsplit("/", 1)[-1]
    return cleaned_id


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
    access_token = (
        access_token
        or os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")
        or os.getenv("WORKSPACE_AGENT_ACCESS_TOKEN")
    )

    if dry_run:
        return {
            "status": "dry_run",
            "agent_id": cleaned_agent_id,
            "conversation_key": conversation_key,
            "idempotency_key": idempotency_key,
            "input_text": input_text,
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
        }

    body = {"input": input_text}
    if conversation_key:
        body["conversation_key"] = conversation_key

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    request = urllib.request.Request(
        f"{WORKSPACE_AGENT_API_BASE_URL}/{cleaned_agent_id}/trigger",
        data=json.dumps(body).encode("utf-8"),
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

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            return {
                "status": "accepted" if status_code == 202 else "failed",
                "http_status": status_code,
                "note": "Workspace Agent trigger accepted. Output must be saved by agent/MCP or loaded from save_address.",
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": "failed",
            "http_status": exc.code,
            "note": "Workspace Agent trigger failed.",
            "error": exc.read().decode("utf-8", errors="replace"),
            "error_type": "http_error",
        }
    except (TimeoutError, socket.timeout) as exc:
        return {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger timed out.",
            "error": str(exc),
            "error_type": "timeout",
        }
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        error_type = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "connection_error"
        return {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger connection failed.",
            "error": str(reason),
            "error_type": error_type,
        }
    except ConnectionError as exc:
        return {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger connection failed.",
            "error": str(exc),
            "error_type": "connection_error",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "http_status": None,
            "note": "Workspace Agent trigger failed.",
            "error": str(exc),
            "error_type": "unexpected_error",
        }
