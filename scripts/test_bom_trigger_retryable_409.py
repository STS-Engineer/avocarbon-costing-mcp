import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import services.choke_sequential_agent_workflow as workflow
from bom_trigger_retry_test_support import (
    accepted_202,
    event_names,
    retryable_409,
    unique_identity,
    write_customer_input,
)


def main():
    os.environ["WORKSPACE_AGENT_TRIGGER_MAX_ATTEMPTS"] = "3"
    os.environ["WORKSPACE_AGENT_TRIGGER_BACKOFF_SECONDS"] = "0,0,0"
    project_code, product_id = unique_identity("BOM-RETRY-409")
    input_file = write_customer_input(project_code, product_id)
    responses = iter([retryable_409(), accepted_202()])
    original_trigger = workflow._trigger
    workflow._trigger = lambda *args, **kwargs: next(responses)
    try:
        result = workflow.start_real_choke_workflow(input_file, dry_run=False)
    finally:
        workflow._trigger = original_trigger

    state = result["state"]
    attempts = state["bom"]["trigger_attempts"]
    assert state["status"] == "bom_triggered", state
    assert state["bom"]["status"] == "triggered", state["bom"]
    assert len(attempts) == 2, attempts
    assert attempts[0]["http_status"] == 409 and attempts[0]["retryable"] is True, attempts
    assert attempts[1]["http_status"] == 202, attempts
    assert state["input_file"] == input_file, state
    assert state["drawing_file_url"], state
    events = event_names(project_code, product_id)
    assert events.count("bom_trigger_attempt") == 2, events
    assert "bom_trigger_accepted" in events, events
    print("PASS retryable HTTP 409 retried and second attempt was accepted")
    print("PASS workflow identity and drawing metadata were preserved")


if __name__ == "__main__":
    main()
