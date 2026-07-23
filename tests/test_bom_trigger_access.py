import json
import urllib.error
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from services import agent_file_proxy_service as proxy
from services import choke_sequential_agent_workflow as workflow


def _trigger_input(project_code="24018-CHO-00", product_id="300440157"):
    return {
        "project_code": project_code,
        "product_id": product_id,
        "product_line": "Chokes",
        "drawing_file_path": (
            "data/customer_inputs/uploads/RFQ-20260722-103043/"
            "0300440157_INDUCTOR_20_04_24.pdf"
        ),
        "drawing_reference": "0300440157_INDUCTOR_20.04.24.pdf",
    }


def test_token_signs_and_verifies_the_final_stored_filename(monkeypatch):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)
    project = "RFQ-20260722-103043"
    stored = "0300440157_INDUCTOR_20_04_24.pdf"
    original = "0300440157_INDUCTOR_20.04.24.pdf"

    token = proxy.create_agent_file_token(project, stored)
    valid = proxy.inspect_agent_file_token(project, stored, token, now_timestamp=1_001)
    mismatch = proxy.inspect_agent_file_token(project, original, token, now_timestamp=1_001)

    assert valid["valid"] is True
    assert valid["normalized_relative_path"] == f"uploads/{project}/{stored}"
    assert valid["signature_message"] == f"uploads/{project}/{stored}\n{token.split('.', 1)[0]}"
    assert mismatch["valid"] is False
    assert mismatch["reason"] == "signature_mismatch"


def test_expired_token_reports_specific_reason(monkeypatch):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)
    token = proxy.create_agent_file_token("P", "drawing.pdf", expiry_seconds=7_200)

    result = proxy.inspect_agent_file_token("P", "drawing.pdf", token, now_timestamp=8_201)

    assert result["valid"] is False
    assert result["reason"] == "expired"
    assert result["expires_at"] == 8_200


def test_expired_proxy_route_returns_expiry_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setattr(proxy, "CUSTOMER_INPUT_DIR", tmp_path)
    monkeypatch.setattr(proxy.time, "time", lambda: 1_000)
    token = proxy.create_agent_file_token("P", "drawing.pdf", expiry_seconds=7_200)
    monkeypatch.setattr(proxy.time, "time", lambda: 8_201)

    with TestClient(app) as client:
        response = client.get(
            "/api/choke-costing/agent-files/P/drawing.pdf",
            params={"token": token},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "expired"


def test_rebuilding_trigger_payload_creates_a_fresh_proxy_token(monkeypatch):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://backend.example.test")
    ticks = iter([1_000, 2_000])
    monkeypatch.setattr(proxy.time, "time", lambda: next(ticks))

    first = workflow._build_bom_trigger_payload("P", "10", _trigger_input("P", "10"))
    second = workflow._build_bom_trigger_payload("P", "10", _trigger_input("P", "10"))

    assert first["drawing_agent_proxy_url"] != second["drawing_agent_proxy_url"]
    assert "/0300440157_INDUCTOR_20_04_24.pdf?" in second["drawing_agent_proxy_url"]


def test_pdf_validation_uses_get_range_when_head_is_not_supported(monkeypatch):
    observed = {}

    class Response:
        status = 206
        headers = {"Content-Type": "application/pdf", "Content-Length": "20"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b"%PDF-1.4\nvalid"

        def geturl(self):
            return "https://backend.example.test/drawing.pdf"

    def fake_urlopen(request, timeout):
        observed["method"] = request.get_method()
        observed["range"] = request.get_header("Range")
        return Response()

    monkeypatch.setattr(proxy.urllib.request, "urlopen", fake_urlopen)
    result = proxy.verify_agent_pdf_url("https://backend.example.test/drawing.pdf")

    assert result["success"] is True
    assert observed == {"method": "GET", "range": "bytes=0-4095"}


def test_proxy_validation_selects_proxy_and_does_not_try_sas(monkeypatch):
    calls = []
    trigger = {
        "payload": {},
        "drawing_url_candidates": [
            {"access_mode": "backend_signed_proxy", "url": "https://proxy", "fresh": True},
            {"access_mode": "azure_blob_sas", "url": "https://blob", "fresh": True},
        ],
    }

    def validate(url):
        calls.append(url)
        return {"success": True, "http_status": 200}

    monkeypatch.setattr(workflow, "verify_agent_pdf_url", validate)
    result = workflow._validate_and_select_drawing_url(trigger)

    assert result["success"] is True
    assert result["selected"]["access_mode"] == "backend_signed_proxy"
    assert calls == ["https://proxy"]


def test_proxy_failure_falls_back_to_valid_fresh_blob_sas(monkeypatch):
    trigger = {
        "payload": {},
        "drawing_url_candidates": [
            {"access_mode": "backend_signed_proxy", "url": "https://proxy", "fresh": True},
            {"access_mode": "azure_blob_sas", "url": "https://blob", "fresh": True},
        ],
    }
    monkeypatch.setattr(
        workflow,
        "verify_agent_pdf_url",
        lambda url: {
            "success": url == "https://blob",
            "http_status": 200 if url == "https://blob" else 403,
        },
    )

    result = workflow._validate_and_select_drawing_url(trigger)

    assert result["success"] is True
    assert result["selected"]["access_mode"] == "azure_blob_sas"
    assert trigger["payload"]["drawing_file_url"] == "https://blob"


def test_both_drawing_urls_failing_blocks_selection(monkeypatch):
    trigger = {
        "payload": {},
        "drawing_url_candidates": [
            {"access_mode": "backend_signed_proxy", "url": "https://proxy", "fresh": True},
            {"access_mode": "azure_blob_sas", "url": "https://blob", "fresh": True},
        ],
    }
    monkeypatch.setattr(
        workflow,
        "verify_agent_pdf_url",
        lambda url: {"success": False, "http_status": 403},
    )

    result = workflow._validate_and_select_drawing_url(trigger)

    assert result["success"] is False
    assert result["rejection_reason"] == "no_accessible_drawing_url"
    assert len(result["candidate_validations"]) == 2


def test_stale_bom_files_do_not_turn_failed_trigger_into_bom_received(monkeypatch, tmp_path):
    raw = tmp_path / "raw.json"
    normalized = tmp_path / "normalized.json"
    raw.write_text(json.dumps({"bom": []}), encoding="utf-8")
    normalized.write_text(json.dumps({"components": []}), encoding="utf-8")
    state = {
        "project_code": "P",
        "product_id": "10",
        "status": "bom_trigger_failed",
        "bom": {"status": "bom_trigger_failed", "retryable": True},
        "missing_outputs": ["bom"],
        "components": {},
        "most": {},
        "errors": [],
    }
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, tmp_path / "state.json"))
    monkeypatch.setattr(workflow, "_bom_raw_path", lambda *args: raw)
    monkeypatch.setattr(workflow, "_bom_normalized_path", lambda *args: normalized)
    monkeypatch.setattr(
        workflow,
        "data_reference_candidates",
        lambda reference: [raw if "raw" in str(reference) else normalized],
    )
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        workflow,
        "workflow_path_diagnostics",
        lambda *args: {
            "resolved_data_root": str(tmp_path),
            "resolved_workflow_state_path": str(tmp_path / "state.json"),
            "process_id": 1,
            "cwd": str(tmp_path),
        },
    )

    result = workflow.get_workflow_state("P", "10")

    assert result["status"] == "bom_trigger_failed"
    assert result["bom"]["status"] == "bom_trigger_failed"
    assert result["missing_outputs"] == ["bom"]
    assert result["stale_previous_output"]["normalized_bom_exists"] is True


def test_retry_records_validation_and_one_agent_attempt(monkeypatch):
    state = {
        "project_code": "P",
        "product_id": "10",
        "status": "bom_trigger_failed",
        "customer_input": _trigger_input("P", "10"),
        "bom": {"status": "bom_trigger_failed", "save_path": "bom.json"},
        "missing_outputs": ["bom"],
    }
    trigger_calls = []
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, Path("state.json")))
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(
        workflow,
        "_build_bom_trigger_payload",
        lambda *args, **kwargs: {
            "input_text": "fresh",
            "save_address": "bom.json",
            "drawing_file_url": "https://fresh",
            "drawing_agent_proxy_url": "https://fresh",
            "drawing_sas_url": None,
            "drawing_access_mode": "backend_signed_proxy",
        },
    )
    monkeypatch.setattr(
        workflow,
        "_validate_and_select_drawing_url",
        lambda trigger: {
            "success": True,
            "selected": {"access_mode": "backend_signed_proxy"},
            "candidate_validations": [{"validation": {"success": True}}],
        },
    )

    def trigger_once(**kwargs):
        trigger_calls.append(kwargs)
        return {
            "status": "accepted",
            "retryable": False,
            "attempts": [{"stage": "agent_trigger", "attempt_number": 1}],
        }

    monkeypatch.setattr(workflow, "_trigger_bom_agent_with_retries", trigger_once)
    result = workflow.retry_bom_agent("P", "10")

    assert len(trigger_calls) == 1
    assert result["status"] == "awaiting_bom_callback"
    assert result["state"]["missing_outputs"] == ["bom"]
    assert [item["stage"] for item in result["trigger_attempts"]] == [
        "drawing_access_validation",
        "agent_trigger",
    ]


def test_active_trigger_retry_is_idempotently_skipped(monkeypatch):
    state = {
        "project_code": "P",
        "product_id": "10",
        "status": "awaiting_bom_callback",
        "bom": {
            "status": "awaiting_bom_callback",
            "lifecycle_status": "awaiting_bom_callback",
            "accepted_at": workflow._now_iso(),
            "trigger_result": {"status": "accepted"},
        },
    }
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, Path("state.json")))

    result = workflow.retry_bom_agent("P", "10")

    assert result["skipped"] is True
    assert result["reason"] == "bom_callback_wait_still_active"


def test_trigger_payload_keeps_product_id_as_canonical_string(monkeypatch):
    monkeypatch.setenv("AGENT_FILE_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://backend.example.test")

    result = workflow._build_bom_trigger_payload("P", 300440157, _trigger_input("P", "300440157"))

    assert result["payload"]["product_id"] == "300440157"
