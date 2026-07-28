import argparse
import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.project_data_paths import COSTING_RUNS_DIR
from services.workspace_agent_trigger_diagnostic import (
    MINIMAL_DIAGNOSTIC_INPUT,
    run_raw_workspace_trigger,
    unique_conversation_key,
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


def _latest_bom_input_text() -> str | None:
    candidates = sorted(
        COSTING_RUNS_DIR.glob("*/*/workflow_state.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        input_text = (state.get("bom") or {}).get("input_text")
        if isinstance(input_text, str) and input_text.strip():
            return input_text
    return None


def _print_result(label: str, result: dict) -> None:
    print(label)
    print(f"HTTP status: {result.get('http_status')}")
    print(
        "request ID headers: "
        + json.dumps(result.get("response_headers") or {}, sort_keys=True)
    )
    print(f"response body length: {result.get('response_body_length')}")
    print(f"Retry-After: {result.get('retry_after')}")
    print(f"elapsed seconds: {result.get('elapsed_seconds')}")
    print(f"classification: {result.get('classification')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Raw one-request Workspace Agent trigger diagnostic."
    )
    parser.add_argument(
        "--bom-payload-file",
        help="Optional JSON/text file containing the exact BOM trigger input.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env()

    cases = [
        ("A. minimal text only", None),
        ("B. conversation_key omitted", None),
        ("C. unique conversation_key", unique_conversation_key()),
    ]
    results = []
    for label, conversation_key in cases:
        result = run_raw_workspace_trigger(
            input_text=MINIMAL_DIAGNOSTIC_INPUT,
            conversation_key=conversation_key,
            timeout_seconds=30,
        )
        results.append(result)
        _print_result(label, result)

    if any(result.get("http_status") == 202 for result in results):
        if args.bom_payload_file:
            bom_input = Path(args.bom_payload_file).read_text(encoding="utf-8")
        else:
            bom_input = _latest_bom_input_text()
        if bom_input:
            result = run_raw_workspace_trigger(
                input_text=bom_input,
                conversation_key=unique_conversation_key(),
                timeout_seconds=30,
            )
            _print_result("D. exact saved BOM input", result)
        else:
            print("D. exact saved BOM input")
            print("HTTP status: not_run")
            print("request ID headers: {}")
            print("response body length: 0")
            print("Retry-After: None")
            print("elapsed seconds: 0")
            print("classification: no_saved_bom_input")

    return 0 if any(result.get("http_status") == 202 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
