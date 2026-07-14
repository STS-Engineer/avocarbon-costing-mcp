import json
import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"start-before-trigger-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
os.environ["AGENT_FILE_SIGNING_SECRET"] = "test-only-signing-secret"
os.environ["PUBLIC_BASE_URL"] = "https://backend.example.test"
sys.path.insert(0, str(ROOT))

from services import choke_sequential_agent_workflow as workflow
from services.project_data_paths import CUSTOMER_INPUT_DIR, atomic_write_json, get_workflow_run_paths


def main():
    project_code = "TEST-START-PERSISTENCE"
    product_id = "TEST-PRODUCT"
    upload_dir = CUSTOMER_INPUT_DIR / "uploads" / project_code
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = upload_dir / "drawing.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nstart persistence test\n%%EOF")
    customer_input = {
        "project_code": project_code,
        "product_id": product_id,
        "workflow_product_id": product_id,
        "product_line": "Chokes",
        "drawing_reference": "drawing.pdf",
        "drawing_file_path": f"data/customer_inputs/uploads/{project_code}/drawing.pdf",
    }
    input_path = CUSTOMER_INPUT_DIR / f"{project_code}_{product_id}.json"
    atomic_write_json(input_path, customer_input)
    observed = {}

    def fake_verify(url, timeout_seconds=15.0):
        state_path = get_workflow_run_paths(project_code, product_id)["workflow_state_path"]
        observed["pdf_checked_before_trigger"] = True
        observed["pdf_url"] = url
        observed["state_exists_at_pdf_check"] = state_path.exists()
        return {
            "success": True,
            "http_status": 200,
            "content_type": "application/pdf",
            "content_length": len(pdf_path.read_bytes()),
        }

    def fake_trigger(**kwargs):
        state_path = get_workflow_run_paths(project_code, product_id)["workflow_state_path"]
        observed["exists"] = state_path.exists()
        observed["state"] = json.loads(state_path.read_text(encoding="utf-8"))
        observed["pdf_was_checked"] = observed.get("pdf_checked_before_trigger") is True
        return {"status": "accepted", "attempts": [], "retryable": False}

    workflow.verify_agent_pdf_url = fake_verify
    workflow._trigger_bom_agent_with_retries = fake_trigger
    workflow.get_master_manufacturing_strategy = lambda *args: {
        "status": "not_found", "production_plant": None
    }
    workflow.get_master_unit_data = lambda *args: {"status": "not_found"}
    result = workflow.start_real_choke_workflow(
        input_file=f"data/customer_inputs/{input_path.name}",
        dry_run=False,
        request_base_url="https://backend.example.test/",
    )
    assert observed.get("exists") is True
    assert observed.get("state_exists_at_pdf_check") is True
    assert observed.get("pdf_was_checked") is True
    assert "/api/choke-costing/agent-files/" in observed["pdf_url"]
    assert "/mcp/api/" not in observed["pdf_url"]
    assert observed["state"]["status"] == "starting"
    assert observed["state"]["input_file"]
    assert observed["state"]["drawing_file_path"]
    assert result["workflow_state_exists_before_trigger"] is True
    print("PASS: canonical workflow state exists before the Agent trigger function is called")


if __name__ == "__main__":
    try:
        main()
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
