import os
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"pdf-live-route-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
os.environ["PUBLIC_BASE_URL"] = "http://testserver/mcp"
os.environ["AGENT_FILE_SIGNING_SECRET"] = "test-only-signing-secret"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import app
from services.agent_file_proxy_service import build_agent_file_url, uploaded_pdf_path
from services.public_url_service import get_public_rest_base_url


def main():
    project_code = "TEST-LIVE-PDF-ROUTE"
    filename = "drawing.pdf"
    expected = b"%PDF-1.4\nlive route test\n%%EOF"
    path = uploaded_pdf_path(project_code, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)

    url = build_agent_file_url(
        get_public_rest_base_url(),
        project_code,
        filename,
        expiry_seconds=7200,
    )
    parsed = urlsplit(url)
    assert parsed.path.startswith("/api/choke-costing/agent-files/"), url
    assert "/mcp/api/" not in url, url

    with TestClient(app) as client:
        response = client.get(f"{parsed.path}?{parsed.query}")
        health_response = client.get("/api/health")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content == expected
    assert health_response.status_code == 200, health_response.text
    health = health_response.json()
    assert health["public_base_url_raw"] == "http://testserver/mcp"
    assert health["public_rest_base_url_resolved"] == "http://testserver"
    assert health["mcp_url"] == "http://testserver/mcp"
    print("PASS: normalized signed PDF proxy route returns application/pdf")


if __name__ == "__main__":
    try:
        main()
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
