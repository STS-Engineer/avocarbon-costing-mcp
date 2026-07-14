import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_FILE_SIGNING_SECRET"] = "test-only-signing-secret"
sys.path.insert(0, str(ROOT))

from services.agent_file_proxy_service import build_agent_file_url
from services.public_url_service import get_public_rest_base_url


def main():
    cases = [
        "https://mcp-costing.azurewebsites.net",
        "https://mcp-costing.azurewebsites.net/",
        "https://mcp-costing.azurewebsites.net/mcp",
    ]
    for configured in cases:
        os.environ["PUBLIC_BASE_URL"] = configured
        resolved = get_public_rest_base_url()
        assert resolved == "https://mcp-costing.azurewebsites.net", (configured, resolved)
        url = build_agent_file_url(
            resolved,
            "TEST-PROJECT",
            "drawing.pdf",
            expiry_seconds=7200,
        )
        assert url.startswith(
            "https://mcp-costing.azurewebsites.net/api/choke-costing/agent-files/"
        ), url
        assert "/mcp/api/" not in url, url
    print("PASS: public REST URLs never contain /mcp/api/")


if __name__ == "__main__":
    main()
