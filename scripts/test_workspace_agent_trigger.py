import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
TRIGGER_API_BASE_URL = "https://api.chatgpt.com/v1/workspace_agents"

AGENT_ENV = {
    "external": "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
    "bom": "CHATGPT_CHOKE_BOM_AGENT_ID",
    "most": "CHATGPT_MOST_AGENT_ID",
}


def load_env():
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH)
        return
    except Exception:
        pass

    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def clean_agent_id(agent_id):
    cleaned = str(agent_id or "").strip().rstrip("/")
    if cleaned.endswith("/trigger"):
        cleaned = cleaned[: -len("/trigger")]
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    return cleaned


def build_default_input(agent_name):
    if agent_name == "external":
        return """Project 24003-CHO-00.
Component ferrite only.
This is one external component only, not a complete choke.
Annual quantity 600000.
Production plant Kunshan.
Destination China.
Save address:
data/costing_runs/24003-CHO-00/316-5001/components/316-5001-ferrite.json"""

    if agent_name == "bom":
        return """Project 24003-CHO-00.
Product Fuse choke.
Part number 316-5001.
Drawing reference 316-5001-1-熔断电感-QS198102-0051 customer confirmed.pdf.
Do not calculate final price.
Create BOM JSON with ferrite, wire, tin, glue.
Save address:
data/costing_runs/24003-CHO-00/316-5001/bom.json"""

    return """Project 24003-CHO-00.
Operation only.
Component-operation work package only.
Operation 10 winding.
Do not process full product.
Do not read SharePoint.
Return one MOST JSON.
Save address:
data/costing_runs/24003-CHO-00/316-5001/most/10-winding.json"""


def print_dry_run(agent_name, agent_id, conversation_key, idempotency_key, input_text):
    print(f"selected agent: {agent_name}")
    print(f"agent id: {agent_id}")
    print(f"conversation_key: {conversation_key}")
    print(f"idempotency_key: {idempotency_key}")
    print("input_text:")
    print(input_text)


def call_workspace_agent(agent_id, token, conversation_key, idempotency_key, input_text):
    body = {
        "conversation_key": conversation_key,
        "input": input_text,
    }
    request = urllib.request.Request(
        f"{TRIGGER_API_BASE_URL}/{agent_id}/trigger",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.getcode()
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_text = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Workspace Agent trigger failed before HTTP response: {exc}")
        return 1

    print(f"HTTP status: {status}")
    if response_text:
        print("response text:")
        print(response_text)
    if status == 202:
        print("Workspace Agent trigger accepted")
        return 0
    return 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Smoke-test ChatGPT Workspace Agent trigger API for AVOCarbon agents."
    )
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_ENV),
        required=True,
        help="Agent to trigger.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print payload only.")
    mode.add_argument("--call-api", action="store_true", help="Call trigger API.")
    return parser.parse_args()


def main():
    args = parse_args()
    load_env()

    env_name = AGENT_ENV[args.agent]
    raw_agent_id = os.getenv(env_name)
    agent_id = clean_agent_id(raw_agent_id)
    token = os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")
    conversation_key = f"avocarbon-test-{args.agent}-24003-CHO-00"
    idempotency_key = f"avocarbon-test-{args.agent}-{uuid.uuid4()}"
    input_text = build_default_input(args.agent)

    if args.dry_run:
        print_dry_run(args.agent, agent_id, conversation_key, idempotency_key, input_text)
        return 0

    if not agent_id.startswith("agtch_"):
        print("This is not a Workspace Agent trigger ID. Expected agtch_...")
        print(f"selected agent: {args.agent}")
        print(f"agent id: {agent_id or '<missing>'}")
        return 1

    if not token:
        print("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN is missing. Cannot call API.")
        print("Token was not printed.")
        return 1

    return call_workspace_agent(agent_id, token, conversation_key, idempotency_key, input_text)


if __name__ == "__main__":
    sys.exit(main())
