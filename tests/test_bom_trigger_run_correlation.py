import json
import shutil
import uuid
from contextlib import nullcontext
from pathlib import Path

from services import choke_sequential_agent_workflow as workflow


def _test_dir():
    path = Path("test_artifacts_bom_correlation") / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def _patch_start_flow(monkeypatch, tmp_path, project_code="P", product_id="X"):
    state = {
        "project_code": project_code,
        "product_id": product_id,
        "status": "created",
        "bom": {"status": "pending"},
        "components": {},
        "most": {},
        "errors": [],
    }
    built_ids = []
    sent_ids = []
    lifecycle = []
    state_path = tmp_path / project_code / product_id / "workflow_state.json"
    raw_path = tmp_path / project_code / product_id / "raw.json"
    normalized_path = tmp_path / project_code / product_id / "normalized.json"

    monkeypatch.setattr(workflow, "ensure_workflow_storage_ready", lambda: {})
    monkeypatch.setattr(
        workflow,
        "_load_customer_input",
        lambda _path: {"_input_file": "input.json"},
    )
    monkeypatch.setattr(workflow, "_resolve_customer_input_context", lambda value: value)
    monkeypatch.setattr(
        workflow,
        "_project_from_input",
        lambda _value: {
            "project_code": project_code,
            "product_id": product_id,
            "normalized_input": {
                "project_code": project_code,
                "product_id": product_id,
                "drawing_file_path": "drawing.pdf",
            },
            "generated_fields": {},
        },
    )
    monkeypatch.setattr(workflow, "resolve_customer_input_path", lambda _value: tmp_path / "input.json")
    monkeypatch.setattr(workflow, "_write_json", lambda *args, **kwargs: "")
    monkeypatch.setattr(workflow, "classify_choke", lambda *args: {})
    monkeypatch.setattr(
        workflow,
        "get_master_manufacturing_strategy",
        lambda *args: {"status": "not_available"},
    )
    monkeypatch.setattr(workflow, "get_master_unit_data", lambda *args: {"status": "not_available"})
    monkeypatch.setattr(workflow, "classification_trace", lambda *args: {})
    monkeypatch.setattr(
        workflow,
        "workflow_path_diagnostics",
        lambda *args: {"project_code": project_code, "product_id": product_id},
    )
    monkeypatch.setattr(workflow, "_run_dir", lambda *args: state_path.parent)
    monkeypatch.setattr(workflow, "_state_path", lambda *args: state_path)
    monkeypatch.setattr(workflow, "_bom_raw_path", lambda *args: raw_path)
    monkeypatch.setattr(workflow, "_bom_normalized_path", lambda *args: normalized_path)
    monkeypatch.setattr(workflow, "_load_state", lambda *args: state)
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, state_path))
    monkeypatch.setattr(workflow, "_existing_bom_output_evidence", lambda *args: {})
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *args, **kwargs: {})
    def save_state(value):
        state.update(value)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return state

    monkeypatch.setattr(workflow, "_save_state", save_state)
    monkeypatch.setattr(
        workflow,
        "_log_bom_lifecycle",
        lambda event, **fields: lifecycle.append((event, dict(fields))),
    )

    def build_payload(*args, **kwargs):
        run_id = kwargs["trigger_run_id"]
        built_ids.append(run_id)
        payload = {
            "project_code": project_code,
            "product_id": product_id,
            "trigger_run_id": run_id,
        }
        return {
            "payload": payload,
            "input_text": json.dumps(payload),
            "save_address": "raw.json",
            "trigger_run_id": run_id,
            "drawing_file_path": "drawing.pdf",
            "drawing_file_url": "https://example.test/drawing.pdf",
            "drawing_agent_proxy_url": "https://example.test/drawing.pdf",
            "drawing_access_mode": "backend_signed_proxy",
            "drawing_blob_url": None,
            "drawing_sas_url": None,
            "warnings": [],
        }

    def trigger_agent(**kwargs):
        sent_ids.append(kwargs["trigger_run_id"])
        state["_sent_invocation"] = dict(kwargs)
        return {"status": "dry_run", "attempts": [], "retryable": False}

    monkeypatch.setattr(workflow, "_build_bom_trigger_payload", build_payload)
    monkeypatch.setattr(workflow, "_trigger_bom_agent_with_retries", trigger_agent)
    return state, built_ids, sent_ids, lifecycle


def test_start_created_persisted_and_sent_ids_are_identical(monkeypatch):
    tmp_path = _test_dir()
    try:
        state, built_ids, sent_ids, lifecycle = _patch_start_flow(monkeypatch, tmp_path)

        result = workflow._start_real_choke_workflow_locked("input.json", dry_run=True)

        created = next(
            fields["trigger_run_id_created"]
            for event, fields in lifecycle
            if event == "trigger_run_id_created"
        )
        persisted = next(
            fields["trigger_run_id_persisted"]
            for event, fields in lifecycle
            if event == "trigger_run_id_persisted"
        )
        assert len(built_ids) == 1
        assert len(sent_ids) == 1
        assert created == persisted == built_ids[0] == sent_ids[0]
        assert state["bom"]["trigger_run_id"] == sent_ids[0]
        assert result["state"]["bom"]["trigger_run_id"] == sent_ids[0]
        expected_key = f"P:X:sequential:bom:{sent_ids[0]}"
        assert state["bom"]["conversation_key"] == expected_key
        assert state["bom"]["idempotency_key"] == expected_key
        assert state["_sent_invocation"]["conversation_key"] == expected_key
        assert state["_sent_invocation"]["idempotency_key"] == expected_key
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_payload_is_built_only_after_trigger_id_persistence(monkeypatch):
    tmp_path = _test_dir()
    try:
        state, _built_ids, _sent_ids, lifecycle = _patch_start_flow(monkeypatch, tmp_path)

        workflow._start_real_choke_workflow_locked("input.json", dry_run=True)

        events = [event for event, _fields in lifecycle]
        assert events.index("trigger_run_id_persisted") < events.index(
            "trigger_run_id_payload_built"
        )
        assert state["bom"]["trigger_run_id"]
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_bom_only_correlation_created_sent_received_expected(monkeypatch):
    tmp_path = _test_dir()
    try:
        state, built_ids, sent_ids, lifecycle = _patch_start_flow(monkeypatch, tmp_path)
        workflow._start_real_choke_workflow_locked("input.json", dry_run=True)
        created = next(
            fields["trigger_run_id_created"]
            for event, fields in lifecycle
            if event == "trigger_run_id_created"
        )
        persisted = state["bom"]["trigger_run_id"]

        def write_json(path, payload):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            return str(path)

        monkeypatch.setattr(workflow, "_write_json", write_json)
        monkeypatch.setattr(
            workflow,
            "normalize_bom",
            lambda *args: {"components": [{"component_id": "ferrite_core"}]},
        )
        monkeypatch.setattr(workflow, "extract_bom_technical_fields", lambda *args: {})
        monkeypatch.setattr(
            workflow,
            "_update_customer_input_from_bom",
            lambda *args: {"status": "skipped", "extracted": {}},
        )
        monkeypatch.setattr(workflow, "_refresh_master_data_for_state", lambda *args: {})
        monkeypatch.setattr(workflow, "build_choke_process_route", lambda *args: {})
        monkeypatch.setattr(workflow, "_required_external_components", lambda *args: [])

        result = workflow.save_bom_output(
            "P",
            "X",
            {"bom": []},
            trigger_run_id=sent_ids[0],
        )
        correlation = next(
            fields
            for event, fields in lifecycle
            if event == "callback_correlation_checked"
        )
        received = correlation["trigger_run_id_received"]
        expected = correlation["trigger_run_id_expected"]

        assert result["success"] is True
        assert created == persisted == built_ids[0] == sent_ids[0] == received == expected
        print(
            "BOM_CORRELATION "
            f"created={created} persisted={persisted} sent={sent_ids[0]} "
            f"received={received} expected={expected}"
        )
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_duplicate_start_does_not_replace_active_trigger_id(monkeypatch):
    active_id = "active-run"
    state = {
        "project_code": "P",
        "product_id": "X",
        "status": "awaiting_bom_callback",
        "workflow_request_id": "request-1",
        "bom": {
            "status": "awaiting_bom_callback",
            "lifecycle_status": "awaiting_bom_callback",
            "trigger_run_id": active_id,
        },
    }
    monkeypatch.setattr(workflow, "_load_customer_input", lambda *args: {})
    monkeypatch.setattr(workflow, "_resolve_customer_input_context", lambda value: value)
    monkeypatch.setattr(
        workflow,
        "_project_from_input",
        lambda value: {
            "project_code": "P",
            "product_id": "X",
            "normalized_input": {},
        },
    )
    monkeypatch.setattr(workflow, "_bom_run_lock", lambda *args: nullcontext())
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, Path("state.json")))
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "_log_bom_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workflow,
        "_start_real_choke_workflow_locked",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    result = workflow.start_real_choke_workflow(
        "input.json",
        workflow_request_id="request-2",
    )

    assert result["duplicate_start"] is True
    assert result["state"]["bom"]["trigger_run_id"] == active_id


def test_two_projects_do_not_share_trigger_ids(monkeypatch):
    first = workflow._build_bom_trigger_payload(
        "P1",
        "X",
        {"drawing_file_url": "https://example.test/1.pdf", "drawing_access_mode": "diagnostic_url"},
        trigger_run_id="run-project-1",
    )
    second = workflow._build_bom_trigger_payload(
        "P2",
        "X",
        {"drawing_file_url": "https://example.test/2.pdf", "drawing_access_mode": "diagnostic_url"},
        trigger_run_id="run-project-2",
    )

    assert first["payload"]["trigger_run_id"] == "run-project-1"
    assert second["payload"]["trigger_run_id"] == "run-project-2"
    assert first["input_text"] != second["input_text"]


def test_payload_builder_does_not_cache_an_old_trigger_id():
    first = workflow._build_bom_trigger_payload(
        "P",
        "X",
        {"drawing_file_url": "https://example.test/a.pdf", "drawing_access_mode": "diagnostic_url"},
        trigger_run_id="old-run",
    )
    second = workflow._build_bom_trigger_payload(
        "P",
        "X",
        {"drawing_file_url": "https://example.test/a.pdf", "drawing_access_mode": "diagnostic_url"},
        trigger_run_id="new-run",
    )

    assert json.loads(first["input_text"])["trigger_run_id"] == "old-run"
    assert json.loads(second["input_text"])["trigger_run_id"] == "new-run"
