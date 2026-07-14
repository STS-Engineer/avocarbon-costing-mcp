import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import services.choke_sequential_agent_workflow as workflow
from bom_trigger_retry_test_support import event_names, retryable_409, unique_identity, write_customer_input


def main():
    os.environ["WORKSPACE_AGENT_TRIGGER_MAX_ATTEMPTS"] = "3"
    os.environ["WORKSPACE_AGENT_TRIGGER_BACKOFF_SECONDS"] = "0,0,0"
    project_code, product_id = unique_identity("BOM-RETRY-EXHAUSTED")
    input_file = write_customer_input(project_code, product_id)
    original_trigger = workflow._trigger
    workflow._trigger = lambda *args, **kwargs: retryable_409()
    try:
        result = workflow.start_real_choke_workflow(input_file, dry_run=False)
    finally:
        workflow._trigger = original_trigger

    state = result["state"]
    assert state["status"] == "bom_trigger_failed_retryable", state
    assert state["status"] != "blocked", state
    assert state["bom"]["status"] == "trigger_failed_retryable", state["bom"]
    assert state["bom"]["retryable"] is True, state["bom"]
    assert len(state["bom"]["trigger_attempts"]) == 3, state["bom"]
    assert state["input_file"] == input_file, state
    assert state["drawing_file_url"], state
    events = event_names(project_code, product_id)
    assert "bom_trigger_failed_retryable" in events, events
    assert "bom_trigger_failed_non_retryable" not in events, events
    print("PASS exhausted HTTP 409 retries produce retryable workflow state")
    print("PASS no generic blocked state was used")


if __name__ == "__main__":
    main()
