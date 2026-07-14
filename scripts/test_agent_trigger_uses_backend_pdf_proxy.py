import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"trigger-proxy-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
os.environ["AGENT_FILE_SIGNING_SECRET"] = "test-only-signing-secret"
os.environ["PUBLIC_BASE_URL"] = "https://backend.example.test"
sys.path.insert(0, str(ROOT))

from services.choke_sequential_agent_workflow import _build_bom_trigger_payload


if __name__ == "__main__":
    try:
        result = _build_bom_trigger_payload(
            "TEST-PROJECT",
            "TEST-PRODUCT",
            {
                "drawing_file_path": "data/customer_inputs/uploads/TEST-PROJECT/drawing.pdf",
                "drawing_sas_url": "https://blob.example.test/drawing.pdf?secret-sas",
                "drawing_reference": "drawing.pdf",
            },
        )
        url = result["payload"]["drawing_file_url"]
        assert "/api/choke-costing/agent-files/" in url, url
        assert "/mcp/api/" not in url, url
        assert "blob.example.test" not in url, url
        print("PASS: BOM Agent trigger prefers the signed backend PDF proxy")
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
