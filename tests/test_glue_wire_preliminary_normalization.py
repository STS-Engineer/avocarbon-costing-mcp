from copy import deepcopy
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
