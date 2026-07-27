import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from services.workspace_agent_client import trigger_workspace_agent


def _safe_run(label):
    trigger_run_id = str(uuid.uuid4())
    result = trigger_workspace_agent(
        agent_id=os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID"),
        access_token=os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN"),
        input_text=json.dumps(
            {
                "trigger_run_id": trigger_run_id,
                "instruction": "Reply with OK only. This is a safe conversation-isolation diagnostic.",
            },
            separators=(",", ":"),
        ),
        conversation_key="avocarbon:bom:conversation-isolation",
        idempotency_key=f"avocarbon:bom:conversation-isolation:{trigger_run_id}",
        dry_run=False,
        timeout_seconds=30,
        conversation_mode="new",
    )
    audit = result.get("invocation_audit") or {}
    print(
        json.dumps(
            {
                "run": label,
                "trigger_run_id": trigger_run_id,
                "status": result.get("status"),
                "http_status": result.get("http_status"),
                "invocation_id": audit.get("invocation_id"),
                "conversation_mode": audit.get("conversation_mode"),
                "conversation_key": audit.get("conversation_key"),
                "returned_conversation_id": audit.get(
                    "returned_conversation_id"
                ),
            },
            indent=2,
        )
    )
    return trigger_run_id, result


def main():
    first_id, first = _safe_run("A")
    second_id, second = _safe_run("B")
    first_audit = first.get("invocation_audit") or {}
    second_audit = second.get("invocation_audit") or {}
    isolated = (
        first_id != second_id
        and first_audit.get("invocation_id")
        != second_audit.get("invocation_id")
        and first_audit.get("conversation_key")
        != second_audit.get("conversation_key")
    )
    print(f"isolated: {str(isolated).lower()}")
    return 0 if isolated and all(
        result.get("http_status") == 202 for result in (first, second)
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
