import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.choke_simulation_service as simulation


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(simulation, "get_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        simulation,
        "get_master_manufacturing_strategy",
        lambda *_: {
            "status": "found",
            "source": "test.product_matrix",
            "production_plant": "Kunshan",
        },
    )
    monkeypatch.setattr(
        simulation,
        "get_master_unit_data",
        lambda *_: {
            "status": "found",
            "source": "test.unit",
            "plant": "Kunshan",
            "operating_currency": "RMB",
            "selling_currency": "RMB",
            "dl_rate_operating_per_hour": 32,
            "voh_rate_operating_per_hour": 9.6,
            "foh_percent_dc": 77,
            "fee_percent_dc": 56,
            "open_hours_per_year": 6000,
        },
    )
    return tmp_path


def context(**updates):
    return {
        "simulation_id": "SIM-TEST",
        "project_code": "24003-CHO-00",
        "product_id": "316-5001",
        "product_line": "Chokes",
        "product": "Fuse choke",
        "destination_zone": "China South Pacific",
        "annual_quantity": 600000,
        "reporting_currency": "RMB",
        **updates,
    }


def bom(quantity=2, glue_status=None):
    components = [{
        "component_id": "ferrite_core",
        "component_name": "Ferrite core",
        "quantity_per_product": quantity,
    }]
    if glue_status:
        components.append({
            "component_id": "glue",
            "component_name": "Glue",
            "quantity_per_product": 1,
            "glue_status": glue_status,
        })
    return {"bom": components}


def component(delivered=0.1, transport=0.01, customs=0.002, forwarder=0.003, **extra):
    return {
        "component_name": "Ferrite core",
        "component_family": "ferrite",
        "recommended_offer": {
            "delivered_cost_per_component": delivered,
            "transportation_cost_per_component": transport,
            "customs_cost_per_component": customs,
            "forwarder_fee_per_component": forwarder,
        },
        "indexed_material_cost_per_component": 0.06,
        "non_indexed_material_cost_per_component": 0.04,
        **extra,
    }


def most(component_id="ferrite_core", strokes=1000, oee=90, operator=50):
    return {
        "component_id": component_id,
        "operations": [{
            "operation_id": f"{component_id}-op",
            "operation_name": "Assembly",
            "strokes_per_hour": strokes,
            "pieces_per_stroke": 1,
            "oee_percent": oee,
            "operator_percent": operator,
            "generic_capex": 1000,
            "specific_capex": 500,
            "tooling_cost": 0,
            "lifetime_warranty": False,
        }],
    }


def complete_simulation(glue_status=None):
    simulation.create_simulation(context())
    simulation.save_output("SIM-TEST", "bom", bom(glue_status=glue_status))
    simulation.save_output("SIM-TEST", "component_costing", component(), "ferrite_core")
    simulation.save_output("SIM-TEST", "most_component", most(), "ferrite_core")


def test_canonical_envelope_validation(isolated):
    envelope = simulation.normalize_output(bom(), "bom", context())
    assert simulation.validate_envelope(envelope, "bom")["valid"]


def test_legacy_component_json_normalization(isolated):
    normalized = simulation.normalize_output({
        "material_cost": 0.12,
        "transportation_cost": 0.01,
        "custom_duty_cost": 0.02,
        "forwarder_cost": 0.03,
    }, "component_costing", context(), "core")
    assert normalized["data"]["recommended_offer"]["delivered_cost_per_component"] == 0.12
    assert normalized["data"]["classification"] == "External"


def test_one_json_per_component_requires_replace(isolated):
    simulation.create_simulation(context())
    simulation.save_output("SIM-TEST", "component_costing", component(), "core")
    with pytest.raises(simulation.SimulationError, match="replace=true"):
        simulation.save_output("SIM-TEST", "component_costing", component(), "core")


def test_bom_quantity_and_logistics_extension(isolated):
    complete_simulation()
    result = simulation.calculate_simulation("SIM-TEST")
    assert result["cost_breakdown"]["material"] == pytest.approx(0.2)
    assert result["cost_breakdown"]["transport"] == pytest.approx(2 * (0.01 + 0.002 + 0.003))


def test_olivier_dl_example(isolated):
    complete_simulation()
    result = simulation.calculate_simulation("SIM-TEST")
    operation = result["calculation_details"]["operations"][0]
    assert operation["effective_pieces_per_hour"] == pytest.approx(900)
    assert operation["direct_labor_hours_per_1000"] == pytest.approx((1000 / 900) * 0.5)
    assert operation["direct_labor_cost_per_piece"] == pytest.approx(((1000 / 900) * 0.5) * 32 / 1000)


def test_olivier_voh_and_foh_fee(isolated):
    complete_simulation()
    result = simulation.calculate_simulation("SIM-TEST")
    operation = result["calculation_details"]["operations"][0]
    assert operation["voh_cost_per_piece"] > 0
    direct = result["cost_breakdown"]["direct_cost"]
    assert result["cost_breakdown"]["foh"] == pytest.approx(direct * 0.77)
    assert result["cost_breakdown"]["fees"] == pytest.approx(direct * 0.56)


@pytest.mark.parametrize(("value", "expected"), [(60, 0.6), (0.6, 0.6), ("60%", 0.6)])
def test_percentage_normalization(value, expected):
    assert simulation.normalize_percent(value) == expected


def test_zero_rate_is_blocking(isolated):
    complete_simulation()
    simulation.update_context("SIM-TEST", {"plant_data_override": {"dl_rate_operating_per_hour": 0}})
    result = simulation.calculate_simulation("SIM-TEST")
    assert result["calculation_status"] == "blocked"
    assert any("dl_rate" in error for error in result["blocking_errors"])


def test_glue_excluded_needs_no_cost_or_most(isolated):
    complete_simulation("excluded_not_required")
    result = simulation.calculate_simulation("SIM-TEST")
    assert not any(error.startswith("component_output:glue") for error in result["blocking_errors"])


def test_glue_assumption_requires_component_cost(isolated):
    complete_simulation("included_assumption")
    result = simulation.calculate_simulation("SIM-TEST")
    assert "component_output:glue" in result["blocking_errors"]


def test_yearly_productivity_added_value(isolated):
    complete_simulation()
    simulation.update_context("SIM-TEST", {"commercial": {"productivity": {"perimeter": "added_value", "yearly_rates": {"SOP+1": 2}}}})
    result = simulation.calculate_simulation("SIM-TEST")
    year = result["yearly_prices"][1]
    assert year["productivity_amount"] == pytest.approx(year["productivity_base"] * 0.02)


def test_simulation_persistence(isolated):
    created = simulation.create_simulation(context())
    loaded = simulation.get_simulation(created["simulation_id"])
    assert loaded["context"]["production_plant"] == "Kunshan"
    assert (isolated / "simulations" / "SIM-TEST" / "context.json").exists()


def test_workflow_import(isolated, monkeypatch, tmp_path):
    workflow_root = tmp_path / "costing_runs" / "P" / "X"
    workflow_root.mkdir(parents=True)
    (workflow_root / "workflow_state.json").write_text(json.dumps({"customer_input": context(simulation_id=None)}), encoding="utf-8")
    (workflow_root / "bom_normalized.json").write_text(json.dumps(bom()), encoding="utf-8")
    monkeypatch.setattr("services.project_data_paths.get_workflow_run_paths", lambda *_: {
        "workflow_state_path": workflow_root / "workflow_state.json",
        "normalized_bom_path": workflow_root / "bom_normalized.json",
        "raw_bom_path": workflow_root / "raw.json",
        "components_dir": workflow_root / "components",
        "most_dir": workflow_root / "most",
    })
    imported = simulation.create_from_workflow("P", "X")
    assert imported["bom"]["output_type"] == "bom"


def test_simulation_api_lifecycle(isolated):
    from app.routers.choke_simulation_router import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    created = client.post("/api/choke-simulation", json=context(simulation_id="SIM-API"))
    assert created.status_code == 200
    assert client.put("/api/choke-simulation/SIM-API/context", json={"target_price": 1.5}).status_code == 200
    assert client.post("/api/choke-simulation/SIM-API/bom", json={"raw_json": bom()}).status_code == 200
    assert client.post("/api/choke-simulation/SIM-API/components/ferrite_core", json={"raw_json": component()}).status_code == 200
    assert client.post("/api/choke-simulation/SIM-API/most/ferrite_core", json={"raw_json": most()}).status_code == 200
    calculated = client.post("/api/choke-simulation/SIM-API/calculate")
    assert calculated.status_code == 200
    assert calculated.json()["cost_breakdown"]["material"] == pytest.approx(0.2)
    assert client.get("/api/choke-simulation/SIM-API/result").status_code == 200
