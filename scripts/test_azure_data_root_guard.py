import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["WEBSITE_SITE_NAME"] = "test-azure-app"
os.environ["WEBSITES_ENABLE_APP_SERVICE_STORAGE"] = "true"
TEST_ROOT = ROOT / "data" / "test_runs" / f"azure-guard-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
sys.path.insert(0, str(ROOT))

from services import project_data_paths


if __name__ == "__main__":
    try:
        project_data_paths.DATA_ROOT_RAW = "/tmp/avocarbon-costing"
        result = project_data_paths.validate_data_root_configuration()
        assert result["healthy"] is False, result
        assert any("not persistent" in item for item in result["errors"]), result
        print("PASS: Azure startup configuration rejects a /tmp DATA_ROOT")
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
