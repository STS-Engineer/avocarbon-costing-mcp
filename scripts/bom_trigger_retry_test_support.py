import json
from datetime import datetime
from pathlib import Path

from services.project_data_paths import COSTING_RUNS_DIR, CUSTOMER_INPUT_DIR, portable_data_reference


def unique_identity(label):
    suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{label}-{suffix}", f"UNKNOWN-PART-{suffix}"


def write_customer_input(project_code, product_id):
    CUSTOMER_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = CUSTOMER_INPUT_DIR / f"__{project_code}_{product_id}.json"
    payload = {
        "project_code": project_code,
        "product_line": "Chokes",
        "product": "Retry test choke",
        "product_id": product_id,
        "workflow_product_id": product_id,
        "part_number": product_id,
        "drawing_reference": "retry-test.pdf",
        "drawing_file_path": f"data/customer_inputs/uploads/{project_code}/retry-test.pdf",
        "drawing_file_url": "https://example.invalid/retry-test.pdf",
        "drawing_access_mode": "azure_blob_sas",
        "drawing_sas_url": "https://example.invalid/retry-test.pdf",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return portable_data_reference(path)


def retryable_409():
    return {
        "status": "failed",
        "http_status": 409,
        "error_type": "http_error",
        "error": '{"error":{"message":"The workspace agent trigger is not currently available."}}',
    }


def accepted_202():
    return {
        "status": "accepted",
        "http_status": 202,
        "note": "Workspace Agent trigger accepted.",
    }


def event_names(project_code, product_id):
    path = COSTING_RUNS_DIR / project_code / product_id / "workflow_events.jsonl"
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [event["event"] for event in events]
