import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


def mask_value(value, prefix_length, suffix_length):
    if not value:
        return "<missing>"
    if len(value) <= prefix_length + suffix_length:
        return "*" * len(value)
    return f"{value[:prefix_length]}...{value[-suffix_length:]}"


def main():
    token = os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN", "").strip()
    agent_id = os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID", "").strip()
    endpoint = (
        f"https://api.chatgpt.com/v1/workspace_agents/{agent_id}/trigger"
    )

    print(f"token_present: {bool(token)}")
    print(f"token_length: {len(token)}")
    print(f"masked_token: {mask_value(token, 6, 4)}")
    print(f"masked_agent_id: {mask_value(agent_id, 10, 6)}")
    print(f"endpoint: {endpoint}")

    if not token or not agent_id:
        print("HTTP status: not_sent")
        print("response body: required environment variable is missing")
        print("elapsed seconds: 0.000")
        return 1

    body = {
        "input": "{\"instruction\":\"Reply with OK only.\"}",
        "conversation_key": "avocarbon-bom-trigger-diagnostic-v1",
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            http_status = response.getcode()
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        response_body = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        http_status = "no_response"
        response_body = str(exc)
    elapsed_seconds = time.perf_counter() - started_at

    print(f"HTTP status: {http_status}")
    print(f"response body: {response_body}")
    print(f"elapsed seconds: {elapsed_seconds:.3f}")
    return 0 if http_status == 202 else 1


if __name__ == "__main__":
    raise SystemExit(main())
