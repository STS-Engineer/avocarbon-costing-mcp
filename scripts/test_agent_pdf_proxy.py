import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"pdf-proxy-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
os.environ["AGENT_FILE_SIGNING_SECRET"] = "test-only-signing-secret"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from services.agent_file_proxy_service import create_agent_file_token, uploaded_pdf_path
from app.main import app


if __name__ == "__main__":
    try:
        project_code = "TEST-PDF-PROXY"
        filename = "drawing.pdf"
        expected = b"%PDF-1.4\nproxy test bytes\n%%EOF"
        path = uploaded_pdf_path(project_code, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        token = create_agent_file_token(project_code, filename, expiry_seconds=7200)
        with TestClient(app) as client:
            response = client.get(
                f"/api/choke-costing/agent-files/{project_code}/{filename}",
                params={"token": token},
            )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.content == expected
        print("PASS: signed Agent PDF proxy returns matching application/pdf bytes")
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
