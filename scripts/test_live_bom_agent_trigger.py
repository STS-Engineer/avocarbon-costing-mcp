import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_sequential_agent_workflow import get_workflow_state, test_bom_agent_trigger


def main():
    parser = argparse.ArgumentParser(description="Safely diagnose the live BOM Workspace Agent trigger.")
    parser.add_argument("--project-code", default="BOM-LIVE-TRIGGER-DIAGNOSTIC")
    parser.add_argument("--product-id", default="DIAGNOSTIC-PART")
    parser.add_argument("--drawing-file-url")
    parser.add_argument(
        "--drawing-from-workflow",
        nargs=2,
        metavar=("PROJECT_CODE", "PRODUCT_ID"),
        help="Read the drawing URL from an existing workflow without printing it.",
    )
    parser.add_argument("--drawing-reference", default="diagnostic-drawing.pdf")
    args = parser.parse_args()
    drawing_file_url = args.drawing_file_url
    if not drawing_file_url and args.drawing_from_workflow:
        source_state = get_workflow_state(*args.drawing_from_workflow)
        source_bom = source_state.get("bom") or {}
        source_input = source_state.get("customer_input") or {}
        drawing_file_url = (
            source_state.get("drawing_file_url")
            or source_bom.get("drawing_file_url")
            or source_input.get("drawing_file_url")
            or source_input.get("drawing_sas_url")
        )
    if not drawing_file_url:
        print(json.dumps({
            "status": "blocked",
            "message": "Provide --drawing-file-url or --drawing-from-workflow PROJECT_CODE PRODUCT_ID.",
        }, indent=2))
        return 0

    result = test_bom_agent_trigger(
        args.project_code,
        args.product_id,
        drawing_file_url,
        args.drawing_reference,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
