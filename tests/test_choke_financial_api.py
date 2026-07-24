from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import choke_workflow_router


def client():
    app = FastAPI()
    app.include_router(choke_workflow_router.router)
    return TestClient(app)


def test_financial_readiness_route_uses_project_routing_convention(monkeypatch):
    monkeypatch.setattr(
        choke_workflow_router,
        "get_financial_readiness",
        lambda project_code, product_id: {
            "project_code": project_code,
            "product_id": product_id,
            "financial_status": "blocked",
            "missing_inputs": ["sop_year"],
        },
    )
    response = client().get(
        "/api/choke-workflow/financial-readiness/24018-CHO-00/300440157"
    )
    assert response.status_code == 200
    assert response.json()["missing_inputs"] == ["sop_year"]


def test_calculate_financial_plan_accepts_flat_contract(monkeypatch):
    captured = {}

    def calculate(project_code, product_id, inputs):
        captured.update({
            "project_code": project_code,
            "product_id": product_id,
            "inputs": inputs,
        })
        return {"financial_status": "ready"}

    monkeypatch.setattr(
        choke_workflow_router, "calculate_saved_financial_plan", calculate
    )
    response = client().post(
        "/api/choke-workflow/calculate-financial-plan",
        json={
            "project_code": "24018-CHO-00",
            "product_id": "300440157",
            "mode": "preliminary",
            "sop_year": 2027,
            "annual_quantities": {"Y0": 360000},
        },
    )
    assert response.status_code == 200
    assert captured["inputs"]["sop_year"] == 2027
    assert captured["inputs"]["annual_quantities"]["Y0"] == 360000


def test_solver_endpoint_uses_same_commercial_contract(monkeypatch):
    monkeypatch.setattr(
        choke_workflow_router,
        "solve_saved_selling_price",
        lambda project_code, product_id, inputs: {
            "project_code": project_code,
            "product_id": product_id,
            "mode": inputs["mode"],
            "convergence_status": "converged",
        },
    )
    response = client().post(
        "/api/choke-workflow/solve-selling-price",
        json={
            "project_code": "P",
            "product_id": "X",
            "mode": "preliminary",
            "profitability_target": {"type": "npv_zero"},
        },
    )
    assert response.status_code == 200
    assert response.json()["convergence_status"] == "converged"


def test_invalid_financial_mode_is_rejected():
    response = client().post(
        "/api/choke-workflow/calculate-financial-plan",
        json={"project_code": "P", "product_id": "X", "mode": "quotation"},
    )
    assert response.status_code == 422


def test_historical_comparison_endpoint_never_feeds_calculation(monkeypatch):
    monkeypatch.setattr(
        choke_workflow_router,
        "save_financial_reference_comparison",
        lambda project_code, product_id, historical, explanations, acceptance, owner: {
            "project_code": project_code,
            "product_id": product_id,
            "historical_values_used_in_calculation": False,
            "validation_owner": owner,
        },
    )
    response = client().post(
        "/api/choke-workflow/compare-financial-reference",
        json={
            "project_code": "24018-CHO-00",
            "product_id": "300440157",
            "historical_values": {"Y0.selling_price": 20},
            "validation_owner": "Olivier",
        },
    )
    assert response.status_code == 200
    assert response.json()["historical_values_used_in_calculation"] is False
