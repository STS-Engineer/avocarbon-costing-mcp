import json

from component_step2_test_support import (
    TEST_ROOT,
    cleanup,
    create_workflow,
    successful_raw,
    workflow,
)


def create_most_ready_workflow():
    project, product = create_workflow()
    for component_id in ["ferrite_core", "magnet_wire", "lead_tinning"]:
        workflow.save_component_output(project, product, component_id, successful_raw(component_id))
    return project, product


def install_most_trigger_spy():
    calls = []

    def trigger(agent_env, fallback_name, input_text, conversation_key, idempotency_key, dry_run):
        payload = json.loads(input_text)
        calls.append({"agent_env": agent_env, "payload": payload, "conversation_key": conversation_key, "idempotency_key": idempotency_key, "dry_run": dry_run})
        return {"status": "dry_run" if dry_run else "accepted", "http_status": None if dry_run else 202}

    workflow._trigger = trigger
    return calls


def successful_most(work_package_id, operation_name="Operation"):
    return {
        "work_package_id": work_package_id,
        "operation_name": operation_name,
        "analysis_status": "complete",
        "method": "BasicMOST",
        "sequence_model": [{"sequence": "A1 B0 G1", "tmus": 20}],
        "tmus": 20,
        "normal_time_seconds": 0.72,
        "allowance_percent": 10,
        "standard_time_seconds": 0.792,
        "pieces_per_hour": 4545.45,
        "oee_percent": 80,
        "operator_count": 1,
        "machine_count": 1,
        "assumptions": [],
        "unconfirmed_values": [],
        "required_confirmations": [],
    }
