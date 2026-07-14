import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"storage-self-test-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
sys.path.insert(0, str(ROOT))

from services.choke_sequential_agent_workflow import run_storage_self_test


if __name__ == "__main__":
    try:
        result = run_storage_self_test()
        assert result["success"] is True, result
        assert result["all_paths_identical"] is True, result
        print("PASS: REST and MCP/shared write-back use the same canonical workflow state path")
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
