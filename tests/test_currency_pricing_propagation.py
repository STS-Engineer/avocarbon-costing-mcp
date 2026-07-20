import math
from pathlib import Path

from services import agent_writeback_service
from services import choke_component_costing as costing
from services import choke_sequential_agent_workflow as workflow
from services.currency_service import (
    convert_currency,
    normalize_currency_code,
    resolve_project_currency,
)


def complete_offer(price, currency, unit):
    return {
        "recommended_offer": {
            "supplier": "Test supplier",
            "unit_price": price,
            "currency": currency,
            "pricing_unit": unit,
            "pricing_basis": f"{currency}/{unit}",
            "incoterm": "DAP",
            "origin": "China",
        }
    }


def test_currency_normalization_and_project_fallback():
    assert normalize_currency_code(" RMB ") == "CNY"
    assert normalize_currency_code("CNY") == "CNY"
    assert normalize_currency_code("") is None
    assert normalize_currency_code("unknown") is None
    assert normalize_currency_code("¥") is None
    assert resolve_project_currency(None, "RMB") == "CNY"


def test_component_currency_never_inherits_from_plant():
    offer = costing.resolve_component_offer({"recommended_offer": {"unit_price": 1, "pricing_unit": "pc"}})
    assert offer["currency"] is None


def test_recommended_offer_currency_priority_and_fallbacks():
    direct = costing.resolve_component_offer({
        "recommended_offer": {"unit_price": 2, "currency": "eur", "unit_price_currency": "USD", "pricing_unit": "pc"},
    })
    assert direct["currency"] == "EUR"
    fallback = costing.resolve_component_offer({
        "recommended_offer": {"unit_price": 2, "unit_price_currency": "inr", "pricing_unit": "pc"},
    })
    assert fallback["currency"] == "INR"


def test_writeback_preserves_recommended_offer_currency_and_unit(monkeypatch):
    raw = {"component_id": "ferrite_core", **complete_offer(0.16, "RMB", "pc")}
    monkeypatch.setattr(agent_writeback_service, "_read_json", lambda *_: raw)
    normalized = agent_writeback_service._normalize_component_output(Path("ferrite_core.json"))
    assert normalized["normalized_offer"]["currency"] == "CNY"
    assert normalized["normalized_offer"]["pricing_unit"] == "pc"
    assert normalized["normalized_cost"]["currency"] == "CNY"


def test_ferrite_piece_quantity_and_cost():
    fields = costing.extract_bom_dimensional_fields("ferrite_core", {"quantity_per_product": 1, "quantity_unit": "pc"})
    quantity = costing.resolve_component_pricing_quantity("ferrite_core", "ferrite", fields, complete_offer(0.16, "CNY", "pc"))
    result = costing.compute_component_material_cost("ferrite_core", quantity, costing.resolve_unit_price(complete_offer(0.16, "CNY", "pc")))
    assert quantity["pricing_quantity"] == 1
    assert result["material_cost_per_product"] == 0.16


def test_wire_explicit_kg_and_derived_kg():
    explicit = costing.resolve_component_pricing_quantity(
        "magnet_wire", "enameled_wire", {"weight_kg_per_product": 0.004}, complete_offer(102, "CNY", "kg"),
    )
    assert explicit["pricing_quantity"] == 0.004
    derived = costing.resolve_component_pricing_quantity(
        "magnet_wire", "enameled_wire",
        {"physical_length_mm_per_product": 336.9255483441646, "diameter_mm": 1.25},
        complete_offer(102, "CNY", "kg"),
    )
    expected_kg = math.pi * (1.25 ** 2) / 4 * 336.9255483441646 * 0.00896 / 1000
    assert math.isclose(derived["pricing_quantity"], expected_kg, rel_tol=1e-12)
    assert derived["pricing_quantity_basis"] == "derived_from_diameter_length_density"


def test_tin_grams_convert_to_supplier_kilograms():
    fields = costing.extract_bom_dimensional_fields(
        "lead_tinning", {"quantity_per_product": 0.0034998724461757923, "quantity_unit": "g"},
    )
    quantity = costing.resolve_component_pricing_quantity("lead_tinning", "tin", fields, complete_offer(450, "CNY", "kg"))
    assert math.isclose(quantity["pricing_quantity"], 0.0034998724461757923 / 1000, rel_tol=1e-12)
    assert quantity["pricing_unit"] == "kg"


def test_length_times_kg_rejected_without_mass_conversion():
    fields = costing.extract_bom_dimensional_fields("magnet_wire", {"quantity_per_product": 0.3, "quantity_unit": "m"})
    quantity = costing.resolve_component_pricing_quantity("magnet_wire", "enameled_wire", fields, complete_offer(102, "CNY", "kg"))
    assert quantity["pricing_quantity"] is None
    result = costing.compute_component_material_cost("magnet_wire", quantity, costing.resolve_unit_price(complete_offer(102, "CNY", "kg")))
    assert result["reason"] == "technical_quantity_unit_unknown"


def test_exchange_rate_conversion_and_missing_rate_block():
    converted = convert_currency(2, "EUR", "CNY", rates={"EUR_to_CNY": 8})
    assert converted["converted_amount"] == 16
    assert converted["rate_source"] == "provided_fx_rates"
    missing = convert_currency(2, "EUR", "CNY", rates={})
    assert missing["status"] == "missing"
    assert missing["reason"] == "exchange_rate_missing"


def test_component_material_converts_before_aggregation():
    material = costing.compute_component_material_cost(
        "ferrite_core", {"pricing_quantity": 1, "pricing_unit": "pc"},
        costing.resolve_unit_price(complete_offer(1, "EUR", "pc")),
    )
    converted = costing.convert_component_cost_to_project_currency(
        "ferrite_core", material, "CNY", fx_rates={"EUR_to_CNY": 8},
    )
    assert converted["material_cost_per_product"] == 8
    assert converted["source_currency"] == "EUR"
    assert converted["currency"] == "CNY"


def test_structured_logistics_can_inherit_offer_currency_only():
    structured = {
        "recommended_offer": {
            "currency": "RMB",
            "transport": {"value": 0.2, "rate_basis": "CNY/pc"},
        }
    }
    resolved = costing.resolve_logistics_value(structured, ["transportation_cost"])
    assert resolved["currency"] == "CNY"
    assert resolved["currency_inherited_from_offer"] is True
    root_scalar = {"currency": "CNY", "transportation_cost": 0.2, "transportation_cost_basis": "CNY/pc"}
    assert costing.resolve_logistics_value(root_scalar, ["transportation_cost"])["currency"] is None


def test_old_incomplete_output_is_marked_and_blocks():
    state = {"project_code": "P", "product_id": "X", "customer_input": {"currency": "RMB"}}
    component = {"component_id": "ferrite_core", "component": "Ferrite", "quantity_per_product": 1}
    normalized = workflow.normalize_component_output(state, component, {"recommended_offer": {"unit_price": 0.16}})
    assert normalized["analysis_status"] == "blocked"
    assert normalized["pricing_completeness"]["requires_regeneration"] is True
    assert normalized["recommended_offer"]["currency"] is None
    assert costing.component_offer_requires_regeneration({"recommended_offer": {"unit_price": 0.16}}) is True
    assert costing.component_offer_requires_regeneration(complete_offer(0.16, "CNY", "pc")) is False


def test_sequential_normalization_preserves_structured_offer():
    state = {"project_code": "P", "product_id": "X", "customer_input": {"currency": "RMB"}}
    component = {"component_id": "ferrite_core", "component": "Ferrite", "quantity_per_product": 1}
    raw = complete_offer(0.16, "RMB", "pc")
    raw["recommended_offer"]["transport"] = {"value": 0.01, "currency": "RMB", "rate_basis": "CNY/pc"}
    normalized = workflow.normalize_component_output(state, component, raw)
    offer = normalized["recommended_offer"]
    assert offer["currency"] == "CNY"
    assert offer["pricing_unit"] == "pc"
    assert offer["transport"]["currency"] == "CNY"
    assert normalized["pricing_completeness"]["status"] == "complete"


def test_external_component_prompt_requires_offer_contract():
    instruction = workflow.COMPONENT_COSTING_INSTRUCTION
    for field in ("currency", "pricing_unit", "pricing_basis", "transport", "customs", "forwarder_fee"):
        assert field in instruction
    assert "Never infer offer currency from the production plant" in instruction
    assert "annual_purchasing_quantity" in instruction


def test_annual_purchasing_quantities_use_supplier_dimensions():
    ferrite_fields = costing.extract_bom_dimensional_fields(
        "ferrite_core", {"quantite": 1, "unite_quantite": "piece"},
    )
    ferrite = costing.resolve_annual_purchasing_quantity("ferrite_core", "ferrite", ferrite_fields, 60000)
    assert ferrite["annual_purchasing_quantity"] == 60000
    assert ferrite["annual_purchasing_unit"] == "pc"

    wire_fields = costing.extract_bom_dimensional_fields(
        "magnet_wire", {"quantite": 1, "poids_par_piece": 3.376, "unite_poids": "g"},
    )
    wire = costing.resolve_annual_purchasing_quantity("magnet_wire", "enameled_wire", wire_fields, 60000)
    assert math.isclose(wire["purchasing_quantity_per_product"], 0.003376, rel_tol=1e-12)
    assert math.isclose(wire["annual_purchasing_quantity"], 202.56, rel_tol=1e-12)
    assert wire["annual_purchasing_unit"] == "kg"

    tin_fields = costing.extract_bom_dimensional_fields(
        "lead_tinning", {"quantite": 2, "poids_par_piece": 0.00818, "unite_poids": "g"},
    )
    tin = costing.resolve_annual_purchasing_quantity("lead_tinning", "tin", tin_fields, 60000)
    assert math.isclose(tin["purchasing_quantity_per_product"], 0.00001636, rel_tol=1e-12)
    assert math.isclose(tin["annual_purchasing_quantity"], 0.9816, rel_tol=1e-12)
    assert tin["annual_purchasing_unit"] == "kg"


def test_component_trigger_payload_contains_both_annual_quantity_bases():
    state = {
        "project_code": "P",
        "product_id": "X",
        "customer_input": {
            "annual_quantity": 60000,
            "currency": "RMB",
            "customer_delivery_zone": "China South Pacific",
        },
        "unit_data": {"selling_currency": "CNY", "plant": "Kunshan"},
    }
    component = {
        "component_id": "magnet_wire",
        "component": "Enameled wire",
        "external_component_type": "enameled_wire",
        "quantity_per_product": 1,
        "component_definition": {"poids_par_piece": 3.376, "unite_poids": "g"},
    }
    payload = workflow._component_trigger_payload(state, component)
    assert payload["annual_product_quantity"] == 60000
    assert math.isclose(payload["annual_purchasing_quantity"], 202.56, rel_tol=1e-12)
    assert payload["annual_purchasing_unit"] == "kg"
    assert payload["reporting_currency"] == "CNY"
    assert payload["costing_scope"] == "raw_enameled_wire_material_only"
    assert "winding" in payload["excluded_costs"]


def test_tin_trigger_excludes_internal_tinning_operation():
    state = {
        "project_code": "P",
        "product_id": "X",
        "customer_input": {"annual_quantity": 60000, "currency": "CNY"},
        "unit_data": {"selling_currency": "CNY"},
    }
    component = {
        "component_id": "lead_tinning",
        "component": "Tin finish",
        "external_component_type": "tin",
        "quantity_per_product": 2,
        "component_definition": {
            "quantite": 2,
            "poids_par_piece": 0.00818,
            "unite_poids": "g",
        },
    }
    payload = workflow._component_trigger_payload(state, component)
    assert payload["costing_scope"] == "tin_consumable_material_only"
    assert payload["annual_purchasing_unit"] == "kg"
    assert math.isclose(payload["annual_purchasing_quantity"], 0.9816, rel_tol=1e-12)
    assert "tinning_operation" in payload["excluded_costs"]
    assert "Never quote subcontract tinning" in payload["instruction"]


def test_synthetic_kunshan_material_result_is_cny(monkeypatch):
    raw_bom = {
        "bom": [
            {"component_id": "ferrite_core", "component_family": "ferrite", "quantity_per_product": 1, "quantity_unit": "pc"},
            {"component_id": "magnet_wire", "component_family": "enameled_wire", "quantity_per_product": 0.3369255483441646, "quantity_unit": "m", "diameter_mm": 1.25},
            {"component_id": "lead_tinning", "component_family": "tin", "quantity_per_product": 0.0034998724461757923, "quantity_unit": "g"},
        ]
    }
    outputs = [
        {"component_id": "ferrite_core", "agent_raw_output": {"component_id": "ferrite_core", **complete_offer(0.16, "CNY", "pc")}},
        {"component_id": "magnet_wire", "agent_raw_output": {"component_id": "magnet_wire", **complete_offer(102, "CNY", "kg")}},
        {"component_id": "lead_tinning", "agent_raw_output": {"component_id": "lead_tinning", **complete_offer(450, "CNY", "kg")}},
    ]
    unit = {
        "status": "found", "plant": "Kunshan", "operating_currency": "CNY", "selling_currency": "CNY",
        "dl_rate_operating_per_hour": 32, "voh_rate_operating_per_hour": 9.6,
        "open_hours_per_year": 5808, "foh_percent_dc": 77, "fee_percent_dc": 56,
    }
    monkeypatch.setattr(workflow, "_load_state", lambda *_: {"customer_input": {"currency": "RMB", "annual_quantity": 60000}, "unit_data": unit})
    monkeypatch.setattr(workflow, "_read_json", lambda *args, **kwargs: raw_bom)
    monkeypatch.setattr(workflow, "_load_saved_component_outputs", lambda *_: outputs)
    monkeypatch.setattr(workflow, "_load_saved_most_outputs", lambda *_: [{"work_package_id": "wp", "p_h": 1000, "oee": 1, "operator_percent": 0.1}])
    result = workflow.calculate_final_choke_costing_from_saved_outputs("P", "X", unit_data_override=unit, fx_rates_override={})

    wire_kg = math.pi * (1.25 ** 2) / 4 * 336.9255483441646 * 0.00896 / 1000
    expected = 0.16 + wire_kg * 102 + (0.0034998724461757923 / 1000) * 450
    assert result["status"] == "calculated"
    assert result["currency"] == "CNY"
    assert math.isclose(result["material_cost_per_piece"], expected, rel_tol=1e-12)
    assert all(item["currency"] == "CNY" for item in result["component_breakdown"])
