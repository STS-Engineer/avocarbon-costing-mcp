"""Regression tests for the Choke component dimensional/logistics costing fix
(Phase 6). Locks down the fix for a regression where a raw BOM quantity
(e.g. a magnet wire's developed length in millimetres) was multiplied
directly against a unit price or logistics rate expressed in an incompatible
unit (RMB/kg), producing costs two to three orders of magnitude too high.

Reference numbers are taken from the real AVOCarbon costing workbook
(24003-CHO-00 - NBT - Fuse Chokes - assy Quotation.xlsm), not from any
specific customer run — no project_code/product_id is hardcoded.
"""

import json
import math

import services.choke_component_costing as costing
import services.material_properties as material_properties
from services.choke_financial_calculation import (
    apply_olivier_direct_foh_fee,
    calculate_dl_voh,
)
from services import choke_sequential_agent_workflow as workflow


# ---------------------------------------------------------------------------
# 1. Explicit wire kg quantity
# ---------------------------------------------------------------------------

def test_wire_explicit_kg_quantity_used_directly():
    bom_fields = {"weight_kg_per_product": 0.00393}
    info = costing.resolve_wire_pricing_quantity(bom_fields)
    assert info["pricing_quantity"] == 0.00393
    assert info["pricing_unit"] == "kg"
    assert info["pricing_quantity_basis"] == "explicit_bom_weight_kg"


# ---------------------------------------------------------------------------
# 2. Wire mass derivation from diameter and length
# ---------------------------------------------------------------------------

def test_wire_mass_derived_from_diameter_and_length():
    mass_g = material_properties.derive_mass_g_from_cylindrical_wire(1.0, 100.0, "copper")
    expected_g = math.pi * 1.0**2 / 4 * 100.0 * 0.00896
    assert math.isclose(mass_g, expected_g, rel_tol=1e-9)

    info = costing.resolve_wire_pricing_quantity({"diameter_mm": 1.0, "physical_length_mm_per_product": 100.0})
    assert info["pricing_unit"] == "kg"
    assert info["pricing_quantity_basis"] == "derived_from_diameter_length_density"
    assert math.isclose(info["pricing_quantity"], expected_g / 1000, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# 3. Rejection of mm x RMB/kg
# ---------------------------------------------------------------------------

def test_developed_length_mm_alone_never_becomes_a_kg_pricing_quantity():
    # No diameter -> mass cannot be derived -> must not fall back to treating
    # the bare mm length as if it were already a kg quantity (the exact bug:
    # 306.93 "mm-ish" length x 0.429 "RMB/kg-ish" price = 131.67).
    info = costing.resolve_wire_pricing_quantity({"physical_length_mm_per_product": 306.93})
    assert info["pricing_quantity"] is None
    assert info["pricing_quantity_basis"] == "unresolved"

    price_info = {"unit_price": 0.429, "unit_price_currency": "RMB", "unit_price_basis": "CNY/kg"}
    result = costing.compute_component_material_cost("magnet_wire", info, price_info)
    assert result["status"] == "blocked"
    assert result["reason"] == "technical_quantity_unit_unknown"


def test_pricing_unit_mismatch_blocks_instead_of_multiplying_incompatible_units():
    info = {"pricing_quantity": 1, "pricing_unit": "pc"}
    price_info = {"unit_price": 79.01, "unit_price_currency": "RMB", "unit_price_basis": "CNY/kg"}
    result = costing.compute_component_material_cost("ferrite_core", info, price_info)
    assert result["status"] == "blocked"
    assert result["reason"] == "pricing_unit_mismatch"
    assert result["physical_unit"] == "pc"


# ---------------------------------------------------------------------------
# 4. Solder/tin mass pricing
# ---------------------------------------------------------------------------

def test_tin_mass_pricing_matches_workbook_reference():
    info = costing.resolve_component_pricing_quantity("lead_tinning", "tinning", {"weight_kg_per_product": 0.00002})
    price_info = {"unit_price": 405, "unit_price_currency": "RMB", "unit_price_basis": "CNY/kg"}
    result = costing.compute_component_material_cost("lead_tinning", info, price_info)
    assert result["status"] == "calculated"
    assert math.isclose(result["material_cost_per_product"], 0.0081, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# 5/6. Separate solder material vs. internal soldering operation; no double-count
# ---------------------------------------------------------------------------

def test_tin_material_component_and_internal_soldering_operation_are_not_duplicated():
    raw_bom = {
        "components": [
            {"id": 1, "product_designation": "Noyau ferrite cylindrique", "component_family": "ferrite", "quantity_per_product": 1},
            {"id": 2, "designation": "Fil cuivre emaille", "quantity_per_product": 1, "turns": 13},
            {"id": 3, "description": "Etamage plomb des broches", "quantity_per_product": 1},
        ]
    }
    normalized = workflow.normalize_bom(raw_bom)
    tin_components = [item for item in normalized["components"] if item["component_id"] == "lead_tinning"]
    assert len(tin_components) == 1  # a single canonical tin/solder material line
    assert tin_components[0]["costing_route"] == "external_component_costing_agent"

    process = workflow.build_most_process_decomposition({"project_code": "T", "product_id": "T", "customer_input": {}}, normalized)
    tinning_ops = [item for item in process["work_packages"] if item["work_package_id"] == "wp_30_lead_tinning"]
    assert len(tinning_ops) == 1  # exactly one internal soldering operation, not one per material line


# ---------------------------------------------------------------------------
# 7/8. Logistics rate-basis compatibility
# ---------------------------------------------------------------------------

def test_logistics_kg_basis_converts_using_matching_pricing_quantity():
    pricing_quantity_info = {"pricing_quantity": 0.00393, "pricing_unit": "kg"}
    agent_raw = {
        "recommended_offer": {
            "transportation_cost_per_piece": 0.006,
            "transportation_cost_per_piece_basis": "CNY/kg",
            "forwarder_cost_per_piece": 0.002,
            "forwarder_cost_per_piece_basis": "CNY/kg",
            "currency": "RMB",
        }
    }
    result = costing.compute_component_transport_cost("magnet_wire", agent_raw, pricing_quantity_info, 0.313)
    assert result["status"] == "calculated"
    assert math.isclose(result["transport_cost_per_product"], 0.00393 * (0.006 + 0.002), rel_tol=1e-9)


def test_logistics_incompatible_basis_blocks_instead_of_multiplying():
    pricing_quantity_info = {"pricing_quantity": 0.00393, "pricing_unit": "kg"}
    agent_raw = {
        "recommended_offer": {
            "transportation_cost_per_piece": 0.006,
            "transportation_cost_per_piece_basis": "CNY/pc",  # quantity is kg, basis is per-piece: incompatible
            "currency": "RMB",
        }
    }
    result = costing.compute_component_transport_cost("magnet_wire", agent_raw, pricing_quantity_info)
    assert result["status"] == "blocked"
    assert result["reason"] == "logistics_rate_basis_incompatible_with_bom_quantity"


# ---------------------------------------------------------------------------
# 9. Blank currency blocks
# ---------------------------------------------------------------------------

def test_blank_currency_blocks_material_cost():
    info = {"pricing_quantity": 1, "pricing_unit": "pc"}
    price_info = {"unit_price": 0.125, "unit_price_currency": None, "unit_price_basis": "CNY/pc"}
    result = costing.compute_component_material_cost("ferrite_core", info, price_info)
    assert result["status"] == "blocked"
    assert result["reason"] == "currency_missing"


def test_blank_currency_blocks_logistics_value():
    pricing_quantity_info = {"pricing_quantity": 1, "pricing_unit": "pc"}
    agent_raw = {"recommended_offer": {"transportation_cost_per_piece": 0.02, "transportation_cost_per_piece_basis": "CNY/pc"}}
    result = costing.compute_component_transport_cost("ferrite_core", agent_raw, pricing_quantity_info)
    assert result["status"] == "blocked"
    assert result["reason"] == "currency_missing"


# ---------------------------------------------------------------------------
# 10. Fixed exchange-rate conversion
# ---------------------------------------------------------------------------

def test_dl_voh_uses_fixed_exchange_rate_when_operating_currency_differs():
    unit_data = {
        "operating_currency": "EUR",
        "selling_currency": "RMB",
        "dl_rate_operating_per_hour": 34,
        "voh_rate_operating_per_hour": 20,
        "open_hours_per_year": 6000,
    }
    fx_rates = {"EUR_to_RMB": 7.7789}
    work_packages = [{"work_package_id": "wp_20_wire_winding", "p_h": 450, "oee": 1.0, "operator_percent": 0.15}]
    result = calculate_dl_voh(work_packages, unit_data, 600000, fx_rates=fx_rates)
    assert result["status"] == "calculated"
    assert result["dl_cost_per_piece"] > 0
    # with no fx_rate for EUR->RMB the same inputs must block, not silently
    # assume a 1:1 rate
    blocked = calculate_dl_voh(work_packages, unit_data, 600000, fx_rates={})
    assert blocked["status"] == "blocked"
    assert "fx_operating_to_selling" in blocked["missing_inputs"]


# ---------------------------------------------------------------------------
# 11. Nested MOST extraction
# ---------------------------------------------------------------------------

def test_nested_station_library_summary_fields_extracted_to_top_level(tmp_path):
    raw_json = {
        "work_package_id": "wp_20_wire_winding",
        "operation_name": "Winding",
        "station_library_summary": {
            "p_h": 450,
            "oee": 0.75,
            "operator_percent": 0.15,
            "generic_capex_eur": 2000,
            "specific_capex_eur": 14000,
            "tooling_cost_eur": 2500,
            "tooling_life_pieces": 250000,
            "tooling_adder_per_piece_eur": 0.002,
        },
    }
    path = tmp_path / "wp_20_wire_winding.json"
    path.write_text(json.dumps(raw_json), encoding="utf-8")
    normalized = workflow._normalize_most_output(path)
    assert normalized["p_h"] == 450
    assert normalized["oee"] == 0.75
    assert normalized["operator_percent"] == 0.15
    assert normalized["generic_capex_eur"] == 2000
    assert normalized["specific_capex_eur"] == 14000
    assert normalized["tooling_cost_eur"] == 2500
    assert normalized["tooling_life_pieces"] == 250000


# ---------------------------------------------------------------------------
# 12. Explicit zero preservation
# ---------------------------------------------------------------------------

def test_external_station_explicit_zero_not_blocked():
    unit_data = {
        "operating_currency": "RMB", "selling_currency": "RMB",
        "dl_rate_operating_per_hour": 34, "voh_rate_operating_per_hour": 20,
        "open_hours_per_year": 6000,
    }
    work_packages = [{
        "work_package_id": "wp_99_external",
        "operation_name": "External step",
        "p_h": 0,
        "operator_percent": 0,
    }]
    result = calculate_dl_voh(work_packages, unit_data, 600000)
    assert result["status"] == "calculated"
    entry = result["work_package_calculation"][0]
    assert entry["status"] == "external_zero_cost"
    assert entry["dl_cost_per_piece"] == 0.0
    assert entry["voh_cost_per_piece"] == 0.0


def test_missing_p_h_still_blocks_unlike_explicit_zero():
    unit_data = {
        "operating_currency": "RMB", "selling_currency": "RMB",
        "dl_rate_operating_per_hour": 34, "voh_rate_operating_per_hour": 20,
        "open_hours_per_year": 6000,
    }
    work_packages = [{"work_package_id": "wp_01", "operation_name": "Unknown", "operator_percent": 0.5}]
    result = calculate_dl_voh(work_packages, unit_data, 600000)
    assert result["status"] == "blocked"
    assert any("p_h" in item for item in result["missing_inputs"])


# ---------------------------------------------------------------------------
# 13/14. Conditional routing matches the workbook reference (no glue, no
# separate electrical test)
# ---------------------------------------------------------------------------

def test_conditional_routing_matches_workbook_reference():
    raw_bom = {
        "components": [
            {"id": 1, "product_designation": "Noyau ferrite cylindrique", "component_family": "ferrite", "quantity_per_product": 1},
            {"id": 2, "designation": "Fil cuivre emaille", "quantity_per_product": 0.00393, "quantity_unit": "kg"},
            {"id": 3, "description": "Etamage plomb", "quantity_per_product": 1},
        ]
    }
    normalized = workflow.normalize_bom(raw_bom)
    process = workflow.build_most_process_decomposition(
        {"project_code": "T", "product_id": "T", "customer_input": {}}, normalized,
    )
    ids = {item["work_package_id"] for item in process["work_packages"]}
    assert ids == {
        "wp_10_ferrite_handling",
        "wp_20_wire_winding",
        "wp_30_lead_tinning",
        "wp_60_visual_inspection_packaging",
    }
    assert "wp_40_glue_application_baking" not in ids
    assert "wp_50_electrical_test" not in ids


def test_electrical_test_included_only_with_explicit_evidence():
    raw_bom = {
        "components": [
            {"id": 1, "product_designation": "Noyau ferrite cylindrique", "component_family": "ferrite", "quantity_per_product": 1},
        ]
    }
    normalized = workflow.normalize_bom(raw_bom)
    process = workflow.build_most_process_decomposition(
        {"project_code": "T", "product_id": "T", "customer_input": {"electrical_test_required": True}},
        normalized,
    )
    ids = {item["work_package_id"] for item in process["work_packages"]}
    assert "wp_50_electrical_test" in ids


# ---------------------------------------------------------------------------
# 15. DL and VOH formulas
# ---------------------------------------------------------------------------

def test_dl_and_voh_formula_matches_hand_calculation():
    unit_data = {
        "operating_currency": "RMB", "selling_currency": "RMB",
        "dl_rate_operating_per_hour": 34, "voh_rate_operating_per_hour": 20,
        "open_hours_per_year": 6000,
    }
    work_packages = [{"work_package_id": "wp_20_wire_winding", "p_h": 450, "oee": 1.0, "operator_percent": 0.15}]
    result = calculate_dl_voh(work_packages, unit_data, 600000)
    entry = result["work_package_calculation"][0]

    hm_mach_per_1000 = 1000 / 450
    expected_dl = hm_mach_per_1000 * 0.15 * 34 / 1000
    assert math.isclose(entry["dl_cost_per_piece"], expected_dl, rel_tol=1e-9)

    expected_voh_base_only = hm_mach_per_1000 * 20 / 1000
    assert entry["voh_cost_per_piece"] >= expected_voh_base_only


# ---------------------------------------------------------------------------
# 16. Plant FOH/Fee formulas (Olivier's preliminary rule)
# ---------------------------------------------------------------------------

def test_foh_and_fee_follow_olivier_plant_percentage_formula():
    dl_voh_result = {"dl_cost_per_piece": 0.07617, "voh_cost_per_piece": 0.10363}
    unit_data = {"foh_percent_dc": 42, "fee_percent_dc": 16}
    transport_result = {"transport_cost_per_piece": 0.0922, "transport_breakdown_by_component": [], "missing_inputs": []}
    result = apply_olivier_direct_foh_fee(dl_voh_result, unit_data, transport_result)

    direct_cost = 0.07617 + 0.10363 + 0.0922
    assert math.isclose(result["direct_cost_per_piece"], direct_cost, rel_tol=1e-9)
    assert math.isclose(result["foh_cost_per_piece"], direct_cost * 0.42, rel_tol=1e-9)
    assert math.isclose(result["fee_cost_per_piece"], direct_cost * 0.16, rel_tol=1e-9)
    assert result["costing_method"] == "preliminary_plant_percentage_dc"


# ---------------------------------------------------------------------------
# 17. Total material close to the workbook reference (~0.46 RMB/pc)
# ---------------------------------------------------------------------------

def test_total_material_close_to_workbook_reference():
    components = [
        ("ferrite_core", 1, "pc", 0.125),
        ("magnet_wire", 0.00393, "kg", 79.01),
        ("solder_paste", 0.00002, "kg", 1065),
        ("lead_tinning", 0.00002, "kg", 405),
    ]
    total = 0.0
    for component_id, qty, unit, price in components:
        info = {"pricing_quantity": qty, "pricing_unit": unit}
        price_info = {"unit_price": price, "unit_price_currency": "RMB", "unit_price_basis": f"CNY/{unit}"}
        result = costing.compute_component_material_cost(component_id, info, price_info)
        assert result["status"] == "calculated"
        total += result["material_cost_per_product"]
    assert math.isclose(total, 0.46, abs_tol=0.01)


# ---------------------------------------------------------------------------
# 18. Final result no longer blocked when inputs are complete
# ---------------------------------------------------------------------------

def test_final_calculation_not_blocked_when_all_inputs_are_complete():
    wire_info = {"pricing_quantity": 0.00393, "pricing_unit": "kg"}
    wire_price = {"unit_price": 79.01, "unit_price_currency": "RMB", "unit_price_basis": "CNY/kg"}
    wire_material = costing.compute_component_material_cost("magnet_wire", wire_info, wire_price)
    assert wire_material["status"] == "calculated"

    wire_transport = costing.compute_component_transport_cost(
        "magnet_wire",
        {
            "recommended_offer": {
                "transportation_cost_per_piece": 0.006,
                "transportation_cost_per_piece_basis": "CNY/kg",
                "forwarder_cost_per_piece": 0.002,
                "forwarder_cost_per_piece_basis": "CNY/kg",
                "currency": "RMB",
            }
        },
        wire_info,
        wire_material["material_cost_per_product"],
    )
    assert wire_transport["status"] == "calculated"

    unit_data = {
        "operating_currency": "RMB", "selling_currency": "RMB",
        "dl_rate_operating_per_hour": 34, "voh_rate_operating_per_hour": 20,
        "open_hours_per_year": 6000, "foh_percent_dc": 42, "fee_percent_dc": 16,
    }
    work_packages = [{"work_package_id": "wp_20_wire_winding", "p_h": 450, "oee": 1.0, "operator_percent": 0.15}]
    dl_voh = calculate_dl_voh(work_packages, unit_data, 600000)
    assert dl_voh["status"] == "calculated"

    final = apply_olivier_direct_foh_fee(dl_voh, unit_data, {
        "transport_cost_per_piece": wire_transport["transport_cost_per_product"],
        "transport_breakdown_by_component": [],
        "missing_inputs": [],
    })
    assert final["direct_cost_per_piece"] is not None
    assert final["direct_cost_per_piece"] > 0
    assert final["manufacturing_cost_per_piece"] is not None
