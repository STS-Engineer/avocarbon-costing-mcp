from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import choke_workflow_router
from services import choke_golden_validation as validation


def test_historical_inputs_and_expected_outputs_are_separate():
    inputs, expected = validation.load_golden_fixtures("choke_24018")

    assert "technical_outputs" not in inputs
    assert "solver_outputs" not in inputs
    assert "technical_outputs" in expected
    assert "solver_outputs" in expected


def test_validation_uses_shared_generic_calculation_functions(monkeypatch):
    called = {"dl_voh": 0}
    original = validation.calculate_dl_voh

    def tracked(*args, **kwargs):
        called["dl_voh"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(validation, "calculate_dl_voh", tracked)
    result = validation.validate_against_golden_reference("choke_24018")

    assert called["dl_voh"] == 1
    assert result["expected_values_used_as_calculation_inputs"] is False
    assert result["production_agents_invoked"] is False
    assert "calculate_dl_voh" in result["generic_engine_result"]["shared_calculation_functions"]
    assert "solve_selling_price" in result["generic_engine_result"]["shared_calculation_functions"]


def test_historical_dl_voh_and_selling_price_are_calculated():
    inputs, expected = validation.load_golden_fixtures("choke_24018")
    result = validation.calculate_generic_choke_costing(inputs)
    technical = result["technical_result"]
    solver = result["selling_price_solver"]

    assert technical["dl_cost_per_piece"] == pytest.approx(
        expected["technical_outputs"]["dl_per_product"]
    )
    assert technical["voh_cost_per_piece"] == pytest.approx(
        expected["technical_outputs"]["voh_per_product"]
    )
    assert len(technical["most_breakdown_by_scope"]) == 4
    assert solver["convergence_status"] == "converged"
    assert solver["solved_y0_selling_price"] is not None


def test_changing_an_input_changes_result_without_mutating_oracle():
    inputs, expected = validation.load_golden_fixtures("choke_24018")
    expected_before = deepcopy(expected)
    baseline = validation.calculate_generic_choke_costing(inputs)
    changed_inputs = deepcopy(inputs)
    changed_inputs["components"][0]["quantity_per_product"] *= 2
    changed = validation.calculate_generic_choke_costing(changed_inputs)

    assert changed["technical_result"]["material_cost_per_piece"] != (
        baseline["technical_result"]["material_cost_per_piece"]
    )
    assert expected == expected_before


def test_unresolved_new_input_is_blocked_explicitly():
    inputs, _ = validation.load_golden_fixtures("choke_24018")
    unresolved = deepcopy(inputs)
    unresolved["components"][0]["unit_price_schedule"] = []
    result = validation.calculate_generic_choke_costing(unresolved)

    assert result["technical_result"]["status"] == "blocked"
    assert "component:magnet_wire" in result["technical_result"]["missing_inputs"]


def test_production_calculate_endpoint_never_loads_golden_fixture(monkeypatch):
    calls = []

    def current_calculator(**kwargs):
        calls.append(kwargs)
        return {"status": "calculated", "source": "current_project"}

    monkeypatch.setattr(
        choke_workflow_router,
        "calculate_final_choke_costing_from_saved_outputs",
        current_calculator,
    )
    monkeypatch.setattr(
        choke_workflow_router,
        "validate_against_golden_reference",
        lambda *_: pytest.fail("production endpoint loaded golden-reference data"),
    )
    response = TestClient(app).post(
        "/api/choke-workflow/calculate-final",
        json={
            "project_code": "NEW-RFQ",
            "product_id": "NEW-PART",
            "mode": "historical_quotation",
        },
    )

    assert response.status_code == 200
    assert response.json()["source"] == "current_project"
    assert calls == [{
        "project_code": "NEW-RFQ",
        "product_id": "NEW-PART",
        "unit_data_override": None,
        "result_mode": "firm",
    }]


def test_golden_validation_endpoint_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_GOLDEN_REFERENCE_VALIDATION", raising=False)
    response = TestClient(app).post(
        "/api/choke-workflow/validate-against-golden-reference",
        json={"reference_id": "choke_24018", "use_historical_inputs": True},
    )

    assert response.status_code == 404


def test_golden_validation_endpoint_returns_calculated_and_expected(monkeypatch):
    monkeypatch.setenv("ENABLE_GOLDEN_REFERENCE_VALIDATION", "true")
    response = TestClient(app).post(
        "/api/choke-workflow/validate-against-golden-reference",
        json={"reference_id": "choke_24018", "use_historical_inputs": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["generic_engine_result"]["engine"] == "generic_choke_costing"
    assert body["expected_excel_result"]["reference_id"] == "choke_24018"
    assert body["reconciliation"]
    assert body["production_agents_invoked"] is False
