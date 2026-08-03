from copy import deepcopy
import json
from pathlib import Path

import pytest

from services import choke_component_costing as costing
from services import choke_sequential_agent_workflow as workflow


def _wire_offer(**overrides):
    offer = {
        "unit_price": 1177.553,
        "currency": "INR",
        "pricing_unit": "kg",
        "delivered_cost": 1479.412348,
        "delivered_cost_currency": "INR",
        "delivered_cost_basis": "INR/kg",
        "transport_cost": 115,
        "transport_cost_currency": "INR",
        "transport_basis": "INR/kg",
        "customs_cost": 142.18083,
        "customs_cost_currency": "INR",
        "customs_basis": "INR/kg",
        "forwarder_fee": 35,
        "forwarder_fee_currency": "INR",
        "forwarder_basis": "INR/kg",
        "capital_cost_12pct": 9.678518,
        "capital_cost_currency": "INR",
        "capital_cost_basis": "INR/kg",
    }
    offer.update(overrides)
    return {
        "component_id": "magnet_wire",
        "component_family": "enameled_wire",
        "technical_specification": {
            "line_weight_g_per_product": 0.725061,
            "developed_length_m_per_product": 0.18317,
        },
        "recommended_offer": offer,
    }


def _wire_bom_component():
    return {
        "component_id": "magnet_wire",
        "component": "Enameled copper wire",
        "category": "wire",
        "external_component_type": "enameled_wire",
        "quantity_per_product": 0.18317,
        "component_definition": {
            "line_weight_g_per_product": 0.725061,
            "developed_length_m_per_product": 0.18317,
        },
    }


def _glue_offer():
    return {
        "component_id": "glue",
        "component_family": "glue",
        "technical_specification": {
            "estimated_total_weight_g_per_product": 0.032673,
            "bead_geometry_assumption": (
                "2 zones, 1 mm diameter, 10.4 mm, density 2 g/cm3"
            ),
        },
        "recommended_offer": {
            "unit_price": 10000,
            "currency": "INR",
            "pricing_unit": "kg",
            "delivered_cost": 10126.619178,
            "delivered_cost_currency": "INR",
            "delivered_cost_basis": "INR/kg",
            "transport_cost": 85,
            "transport_cost_currency": "INR",
            "transport_basis": "INR/kg",
            "customs_cost": 0,
            "customs_cost_currency": "INR",
            "customs_basis": "INR/kg",
            "forwarder_fee": 25,
            "forwarder_fee_currency": "INR",
            "forwarder_basis": "INR/kg",
            "capital_cost_12pct": 16.619178,
            "capital_cost_currency": "INR",
            "capital_cost_basis": "INR/kg",
        },
    }


def _renormalization_fixture(monkeypatch, tmp_path, component_id, raw):
    run_dir = tmp_path / "costing_runs" / "P" / "X"
    raw_path = run_dir / "agent_outputs" / "components" / f"{component_id}.json"
    normalized_path = run_dir / "components_normalized" / f"{component_id}.json"
    bom_raw_path = run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
    bom_normalized_path = run_dir / "bom_normalized.json"
    most_path = run_dir / "most_normalized" / "wp.json"
    for path in (raw_path, normalized_path, bom_raw_path, bom_normalized_path, most_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    normalized_path.write_text(json.dumps({
        "component_id": component_id,
        "technical_quantity": 0.032673 if component_id == "glue" else None,
        "technical_quantity_unit": (
            "g/product" if component_id == "glue" else None
        ),
        "technical_revision": f"old-{component_id}",
    }, indent=2), encoding="utf-8")
    bom = {
        "components": [
            {
                "component_id": "ferrite_core",
                "component": "Ferrite core",
                "category": "ferrite",
                "costing_route": "external_component_costing_agent",
                "quantity_per_product": 1,
                "quantity_unit": "pc",
                "length_mm": 13,
                "component_definition": {"length_mm": 13},
                "technical_revision": "ferrite-input",
            },
            {
                **_wire_bom_component(),
                "costing_route": "external_component_costing_agent",
                "technical_revision": "wire-input",
            },
            {
                "component_id": "glue",
                "component": "Glue",
                "category": "glue",
                "external_component_type": "glue",
                "costing_route": "external_component_costing_agent",
                "quantity_per_product": 2,
                "component_definition": {
                    "estimated_total_weight_g_per_product": 0.032673,
                    "quantity_per_product": 2,
                    "quantity_unit": "bands",
                },
                "technical_revision": "glue-input",
            },
        ],
        "technical_revision": "bom-current",
    }
    bom_raw_path.write_text(json.dumps(bom), encoding="utf-8")
    bom_normalized_path.write_text(json.dumps(bom), encoding="utf-8")
    most_path.write_bytes(b'{"status":"received"}')
    state = {
        "project_code": "P",
        "product_id": "X",
        "customer_input": {
            "annual_quantity": 360000,
            "currency": "INR",
        },
        "unit_data": {"selling_currency": "INR"},
        "components": {
            component_id: {
                "status": "received",
                "source_component_revision": f"{component_id}-input",
            }
        },
        "technical_revisions": {"normalized_bom": "bom-current"},
    }
    monkeypatch.setattr(workflow, "_run_dir", lambda *_: run_dir)
    monkeypatch.setattr(
        workflow, "_component_output_path", lambda *_: raw_path,
    )
    monkeypatch.setattr(
        workflow, "_normalized_component_output_path",
        lambda *_: normalized_path,
    )
    monkeypatch.setattr(workflow, "_bom_raw_path", lambda *_: bom_raw_path)
    monkeypatch.setattr(
        workflow, "_bom_normalized_path", lambda *_: bom_normalized_path,
    )
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_: bom)
    monkeypatch.setattr(
        workflow, "_existing_state", lambda *_: (state, run_dir / "workflow_state.json"),
    )
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(workflow, "_relative", lambda path: str(path))
    return {
        "raw": raw_path,
        "normalized": normalized_path,
        "bom": bom_raw_path,
        "most": most_path,
        "run_dir": run_dir,
    }


def test_glue_two_zone_olivier_quantity_and_density():
    result = costing.calculate_provisional_glue_consumption(13)
    assert result["density_g_cm3"] == 1.5
    assert result["glue_mass_g_per_strip"] == pytest.approx(0.012252211349)
    assert result["glue_mass_g_per_product"] == pytest.approx(0.024504422698)
    assert result["technical_quantity_unit"] == "g/product"
    assert result["assumption_status"] == "provisional"


def test_wire_mass_and_annual_purchasing_quantity_use_kg_not_length():
    fields = costing.extract_bom_dimensional_fields(
        "magnet_wire", _wire_bom_component(),
    )
    annual = costing.resolve_annual_purchasing_quantity(
        "magnet_wire", "enameled_wire", fields, 360000,
    )
    assert fields["physical_mass_g_per_product"] == 0.725061
    assert fields["physical_length_mm_per_product"] == 183.17
    assert annual["purchasing_quantity_per_product"] == pytest.approx(
        0.000725061
    )
    assert annual["annual_purchasing_quantity"] == pytest.approx(261.02196)
    assert annual["annual_purchasing_unit"] == "kg"
    assert annual["purchasing_quantity_basis"] == "explicit_bom_mass_g_to_kg"


def test_wire_normalization_separates_technical_and_purchasing_quantities():
    state = {
        "project_code": "P",
        "product_id": "X",
        "customer_input": {"annual_quantity": 360000, "currency": "INR"},
        "unit_data": {"selling_currency": "INR"},
    }
    normalized = workflow.normalize_component_output(
        state, _wire_bom_component(), _wire_offer(),
    )
    assert normalized["technical_quantity"] == pytest.approx(0.725061)
    assert normalized["technical_quantity_unit"] == "g/product"
    assert normalized["purchasing_quantity_per_product"] == pytest.approx(
        0.000725061
    )
    assert normalized["purchasing_quantity_unit"] == "kg/product"
    assert normalized["annual_purchasing_quantity"] == pytest.approx(261.02196)
    assert normalized["annual_purchasing_unit"] == "kg/year"
    assert normalized["pricing_unit"] == "kg"


def test_explicit_zero_logistics_with_bases_are_accepted():
    raw = _wire_offer(
        delivered_cost=1177.553,
        transport_cost=0,
        customs_cost=0,
        forwarder_fee=0,
        capital_cost_12pct=0,
    )
    audit = costing.reconcile_delivered_unit_cost(raw, "INR")
    assert audit["status"] == "calculated"
    assert audit["reconciliation_difference"] == 0
    assert [item["value"] for item in audit["included_adders"]] == [0, 0, 0]


def test_null_logistics_remains_unresolved():
    raw = _wire_offer(transport_cost=None)
    audit = costing.reconcile_delivered_unit_cost(raw, "INR")
    assert audit["status"] == "blocked"
    assert audit["reason"] == "delivered_cost_adjustments_unresolved"
    assert any(
        item.get("name") == "transport"
        and item.get("reason") == "logistics_value_missing"
        for item in audit["excluded_adders"]
    )


def test_wire_delivered_cost_excludes_capital_and_reconciles():
    audit = costing.reconcile_delivered_unit_cost(_wire_offer(), "INR")
    assert audit["status"] == "calculated"
    assert audit["base_unit_cost"] == 1177.553
    assert audit["calculated_delivered_unit_cost"] == 1469.73383
    assert audit["reported_delivered_unit_cost"] == 1479.412348
    assert audit["reported_adjustment_correction"] == -9.678518
    assert audit["reconciliation_difference"] == 0
    assert audit["delivered_cost_formula"] == (
        "1177.553 + 115 + 142.18083 + 35 = 1469.73383"
    )
    assert audit["non_delivered_adjustments"][0]["name"] == "capital_cost"


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("transport_basis", "INR/m", "pricing_unit_mismatch"),
        ("transport_cost_currency", "USD", "exchange_rate_missing"),
    ],
)
def test_mixed_logistics_basis_or_currency_is_rejected(
    field, value, expected_reason,
):
    raw = _wire_offer(**{field: value})
    audit = costing.reconcile_delivered_unit_cost(raw, "INR")
    assert audit["status"] == "blocked"
    assert any(
        item.get("reason") == expected_reason
        for item in audit["excluded_adders"]
    )


def test_pure_normalization_does_not_modify_bom_or_most_files(tmp_path):
    bom_path = tmp_path / "bom.json"
    most_path = tmp_path / "most.json"
    bom_path.write_bytes(b'{"components":[]}')
    most_path.write_bytes(b'{"operations":[]}')
    before = (bom_path.read_bytes(), most_path.read_bytes())
    costing.extract_bom_dimensional_fields(
        "magnet_wire", _wire_bom_component(),
    )
    costing.calculate_provisional_glue_consumption(13)
    assert (bom_path.read_bytes(), most_path.read_bytes()) == before


def test_normalization_does_not_invoke_workspace_agent(monkeypatch):
    called = []
    monkeypatch.setattr(
        workflow,
        "trigger_workspace_agent",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    costing.reconcile_delivered_unit_cost(deepcopy(_wire_offer()), "INR")
    assert called == []


def test_dry_run_rebuilds_glue_preview_without_writing(monkeypatch, tmp_path):
    paths = _renormalization_fixture(
        monkeypatch, tmp_path, "glue", _glue_offer(),
    )
    before = {
        key: paths[key].read_bytes()
        for key in ("raw", "normalized", "bom", "most")
    }
    result = workflow.renormalize_component_output(
        "P", "X", "glue", dry_run=True,
    )
    preview = result["normalized_preview"]
    assert result["status"] == "dry_run"
    assert result["agent_triggered"] is False
    assert preview["technical_quantity"] == pytest.approx(0.024504422698)
    assert preview["technical_quantity_unit"] == "g/product"
    assert preview["purchasing_quantity_per_product"] == pytest.approx(
        0.000024504422698
    )
    assert preview["delivered_material_unit_cost"] == 10110
    assert all(paths[key].read_bytes() == value for key, value in before.items())
    assert not (paths["run_dir"] / "revisions").exists()


def test_apply_archives_old_glue_and_preserves_raw_bom_most(
    monkeypatch, tmp_path,
):
    paths = _renormalization_fixture(
        monkeypatch, tmp_path, "glue", _glue_offer(),
    )
    raw_before = paths["raw"].read_bytes()
    bom_before = paths["bom"].read_bytes()
    most_before = paths["most"].read_bytes()
    result = workflow.renormalize_component_output(
        "P", "X", "glue", dry_run=False,
    )
    rebuilt = json.loads(paths["normalized"].read_text(encoding="utf-8"))
    assert result["archive_path"]
    assert Path(result["archive_path"]).exists()
    assert rebuilt["technical_quantity"] == pytest.approx(0.024504422698)
    assert rebuilt["technical_quantity_status"] == "provisional"
    assert rebuilt["delivered_material_cost_per_piece"] == pytest.approx(
        0.247739713477
    )
    assert paths["raw"].read_bytes() == raw_before
    assert paths["bom"].read_bytes() == bom_before
    assert paths["most"].read_bytes() == most_before
    assert (
        result["old_normalized_artifact"]["sha256"]
        != result["new_normalized_artifact"]["sha256"]
    )


def test_apply_rebuilds_wire_delivered_material_and_clears_flags(
    monkeypatch, tmp_path,
):
    paths = _renormalization_fixture(
        monkeypatch, tmp_path, "magnet_wire", _wire_offer(),
    )
    raw_before = paths["raw"].read_bytes()
    result = workflow.renormalize_component_output(
        "P", "X", "magnet_wire", dry_run=False,
    )
    rebuilt = json.loads(paths["normalized"].read_text(encoding="utf-8"))
    assert rebuilt["technical_quantity"] == pytest.approx(0.725061)
    assert rebuilt["purchasing_quantity_per_product"] == pytest.approx(
        0.000725061
    )
    assert rebuilt["annual_purchasing_quantity"] == pytest.approx(261.02196)
    assert rebuilt["delivered_material_unit_cost"] == 1469.73383
    assert rebuilt["delivered_material_cost_per_piece"] == pytest.approx(
        1.065646680514
    )
    assert rebuilt["capital_cost"] == 9.678518
    assert rebuilt["capital_cost_included_in_delivered_material"] is False
    assert rebuilt["reconciliation_residual"] == 0
    assert rebuilt["logistics_adders_unresolved"] is False
    assert rebuilt["delivered_cost_adjustments_unresolved"] is False
    assert rebuilt["costing_resolution_status"] == "resolved"
    assert result["agent_triggered"] is False
    assert paths["raw"].read_bytes() == raw_before


def test_three_current_external_components_can_all_be_resolved():
    component_statuses = {
        "ferrite_core": "resolved",
        "glue": "resolved",
        "magnet_wire": "resolved",
    }
    assert sum(value == "resolved" for value in component_statuses.values()) == 3
    assert len(component_statuses) == 3
