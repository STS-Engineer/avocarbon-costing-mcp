import argparse
import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.workspace_agent_trigger_diagnostic import (
    AGENT_ID_ENVIRONMENTS,
    MINIMAL_DIAGNOSTIC_INPUT,
    run_raw_workspace_trigger,
    safe_trigger_error,
)


def load_env() -> None:
    env_path = ROOT_DIR / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:
        pass
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one minimal Workspace Agent API-channel diagnostic."
    )
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_ID_ENVIRONMENTS),
        required=True,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env()
    result = run_raw_workspace_trigger(
        agent_type=args.agent,
        input_text=MINIMAL_DIAGNOSTIC_INPUT,
        timeout_seconds=30,
    )
    response = result.get("response_body")
    if isinstance(response, str) and response:
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            response = None
    conversation_url_present = bool(
        isinstance(response, dict) and response.get("conversation_url")
    )
    print(json.dumps({
        "agent_type": args.agent,
        "agent_id_prefix": "agtch_"
        if result.get("agent_id_suffix")
        else None,
        "agent_id_suffix": result.get("agent_id_suffix"),
        "http_status": result.get("http_status"),
        "request_id": next(
            iter((result.get("response_headers") or {}).values()),
            None,
        ),
        "conversation_url_present": conversation_url_present,
        "error": safe_trigger_error(result),
        "classification": result.get("classification"),
        "checked_at": result.get("checked_at"),
    }, indent=2))
    return 0 if result.get("http_status") == 202 else 1


if __name__ == "__main__":
    raise SystemExit(main())
