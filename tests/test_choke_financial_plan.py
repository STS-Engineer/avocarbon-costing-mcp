from decimal import Decimal

import pytest

from services.choke_financial_plan import (
    build_year_structure,
    calculate_financial_plan,
    financial_readiness,
    solve_selling_price,
)


def technical(unresolved=True):
    return {
        "project_code": "24018-CHO-00",
        "product_id": "300440157",
        "currency": "INR",
        "status": "preliminary_incomplete" if unresolved else "calculated",
        "material_cost_per_piece": 3.6405967874604075,
        "transport_cost_per_piece": 0.49170253441327726,
        "delivered_material_cost_per_piece": 4.132299321873685,
        "dl_cost_per_piece": 2.329861111111111,
        "voh_cost_per_piece": 1.17477265625,
        "foh_percent_dc": 132,
        "foh_cost_per_piece": 5.275163918342192,
        "fee_percent_dc": 70,
        "fee_cost_per_piece": 2.7974354112420716,
        "unresolved_material_components": (
            [{"component_id": "glue", "reason": "technical_quantity_unit_unknown"}]
            if unresolved else []
        ),
        "component_breakdown": [
            {
                "component_id": "ferrite_core",
                "status": "resolved",
                "currency": "INR",
                "material_cost_per_piece": 2.274288,
                "delivered_material_cost_per_piece": 2.738284,
                "normalized_offer": {},
            },
            {
                "component_id": "magnet_wire",
                "status": "resolved",
                "currency": "INR",
                "material_cost_per_piece": 1.340,
                "delivered_material_cost_per_piece": 1.364940,
                "normalized_offer": {},
            },
            {
                "component_id": "lead_tinning",
                "status": "resolved",
                "currency": "INR",
                "material_cost_per_piece": 0.0263087874604075,
                "delivered_material_cost_per_piece": 0.0290753218736848,
                "normalized_offer": {},
            },
        ],
    }


def commercial(mode="preliminary"):
    supplier_terms = {
        component: {
            "payment_days": 60,
            "incoterm": "FCA",
            "zone_relation": "different",
            "supplier": f"{component} supplier",
        }
        for component in ("ferrite_core", "magnet_wire", "lead_tinning")
    }
    return {
        "mode": mode,
        "sop_year": 2027,
        "annual_quantities": {
            "Y-1": 0,
            "Y0": 360000,
            "Y1": 360000,
            "Y2": 360000,
            "Y3": 360000,
            "Y4": 360000,
            "Y5": 360000,
            "Y6": 360000,
        },
        "y_minus_1_quantity_zero": True,
        "initial_selling_price": 20,
        "customer_productivity": {
            "percentage": 2,
            "start_year": 1,
            "duration": 3,
            "basis": "added_value",
        },
        "material_indexation_rates": {},
        "plant_indexation_rates": {},
        "fx_adjustment_rates": {},
        "customer_payment_days": 45,
        "customer_incoterm": "FCA",
        "customer_delivery_frequency_days": 7,
        "customer_transit_days": 0,
        "platform": False,
        "production_plant": "Chennai",
        "tax_rate": 25,
        "discount_rate": 12,
        "financing_rate": 8,
        "profitability_target": {"type": "npv_zero", "value": 0},
        "capex_tooling_treatment": {
            "generic_capex": {"type": "avocarbon_owned"},
            "specific_capex": {"type": "avocarbon_owned"},
            "tooling": {"type": "prepaid"},
        },
        "investment_fx_rates": {"EUR": 90},
        "ap_value_basis": "base_purchase_value",
        "wip_value_basis": "delivered_material_plus_conversion",
        "supplier_terms": supplier_terms,
        "business_link_values": {},
    }


def assets():
    return [
        {
            "source_id": "wp_winding",
            "category": "generic_capex",
            "amount": 12000,
            "currency": "EUR",
        },
        {
            "source_id": "wp_winding",
            "category": "specific_capex",
            "amount": 3500,
            "currency": "EUR",
        },
        {
            "source_id": "wp_winding",
            "category": "tooling",
            "amount": 2500,
            "currency": "EUR",
        },
    ]


def calculated(**changes):
    inputs = commercial()
    inputs.update(changes)
    return calculate_financial_plan(technical(), inputs, {}, investment_assets=assets())


def row(result, period):
    return next(item for item in result["annual_table"] if item["period"] == period)


def test_missing_sop_blocks_annual_plan():
    inputs = commercial()
    inputs.pop("sop_year")
    result = calculate_financial_plan(technical(), inputs)
    assert result["financial_status"] == "blocked"
    assert "sop_year" in result["missing_inputs"]
    assert result["annual_table"] == []


def test_year_structure_is_y_minus_1_through_y6():
    years = build_year_structure(2027)
    assert [item["period"] for item in years] == [
        "Y-1", "Y0", "Y1", "Y2", "Y3", "Y4", "Y5", "Y6"
    ]
    assert [item["calendar_year"] for item in years] == list(range(2026, 2034))


def test_flat_quantity_is_only_used_when_explicit():
    inputs = commercial()
    inputs.pop("annual_quantities")
    inputs["quantity_rule"] = "flat"
    inputs["flat_annual_quantity"] = 360000
    inputs["y_minus_1_quantity_zero"] = True
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    assert row(result, "Y0")["quantity"] == 360000
    assert any("explicitly selected" in item for item in result["assumptions"])


def test_current_technical_cost_handoff_and_bases_are_preserved():
    result = calculated()
    costs = result["cost_structure"]
    assert costs["base_material"] == pytest.approx(3.640596787)
    assert costs["logistics"] == pytest.approx(0.491702534)
    assert costs["added_value_direct"] == pytest.approx(3.996336302)
    assert costs["total_before_commercial"] == pytest.approx(
        3.6405967874604075 + 3.996336301774388 + 5.275163918342192 + 2.7974354112420716
    )
    assert result["foh_basis"] == "added_value_direct_cost"
    assert result["fee_basis"] == "added_value_direct_cost"


def test_sales_and_operating_pnl_reconcile_without_double_logistics():
    result = calculated()
    y0 = row(result, "Y0")
    assert y0["sales"] == pytest.approx(360000 * 20)
    assert y0["gmdc"] == pytest.approx(
        y0["sales"] - y0["material"] - y0["transport"] - y0["dl"] - y0["voh"]
    )
    assert y0["ebitda"] == pytest.approx(y0["gmdc"] - y0["foh"] - y0["fee"])


def test_productivity_applies_only_to_added_value_for_configured_years():
    result = calculated()
    y0 = row(result, "Y0")
    y1 = row(result, "Y1")
    expected = result["cost_structure"]["manufacturing_added_value"] * 0.02
    assert y0["price_trace"]["productivity_adjustment"] == 0
    assert y1["price_trace"]["productivity_adjustment"] == pytest.approx(expected)
    assert y1["selling_price"] == pytest.approx(20 - expected)


def test_material_indexation_is_separate_from_productivity():
    inputs = commercial()
    inputs["material_indexation_rates"] = {"Y1": 5}
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    y1 = row(result, "Y1")
    assert y1["price_trace"]["material_indexation_adjustment"] > 0
    assert y1["price_trace"]["productivity_basis"] == "added_value"


def test_ar_uses_sales_and_customer_days():
    y0 = row(calculated(), "Y0")
    assert y0["ar"] == pytest.approx(y0["sales"] / 365 * 45)


def test_ap_is_component_level_and_uses_supplier_days():
    y0 = row(calculated(), "Y0")
    assert len(y0["ap_component_breakdown"]) == 3
    assert y0["ap"] == pytest.approx(
        sum(item["annual_purchase_value"] / 365 * 60 for item in y0["ap_component_breakdown"])
    )


def test_supplier_terms_are_not_replaced_by_customer_terms():
    result = calculated()
    y0 = row(result, "Y0")
    assert {item["payment_days"] for item in y0["ap_component_breakdown"]} == {60.0}
    assert result["financial_status"] == "preliminary_incomplete"


def test_fca_different_zone_inventory_rules():
    y0 = row(calculated(), "Y0")
    first = y0["ap_component_breakdown"][0]
    assert first["rm_transit_days"] == 40
    assert first["rm_in_house_days"] == pytest.approx(34)
    assert y0["rm_transit"] > 0


def test_ddp_has_no_raw_material_transit():
    inputs = commercial()
    for value in inputs["supplier_terms"].values():
        value["incoterm"] = "DDP"
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    assert row(result, "Y0")["rm_transit"] == 0


def test_choke_wip_defaults_to_exactly_five_days():
    result = calculated()
    assert row(result, "Y0")["stock_days"]["wip"] == 5


def test_twc_and_delta_twc_reconcile():
    result = calculated()
    y0 = row(result, "Y0")
    y1 = row(result, "Y1")
    assert y0["twc"] == pytest.approx(y0["ar"] + y0["total_inventory"] - y0["ap"])
    assert y0["delta_twc"] == pytest.approx(y0["twc"])
    assert y1["delta_twc"] == pytest.approx(y1["twc"] - y0["twc"])


def test_capex_is_in_y_minus_1_and_depreciation_has_five_charges():
    result = calculated()
    yminus = row(result, "Y-1")
    assert yminus["generic_capex"] == 12000 * 90
    assert yminus["specific_capex"] == 3500 * 90
    assert yminus["tooling_expenditure"] == 2500 * 90
    charges = [row(result, f"Y{year}")["depreciation"] for year in range(7)]
    assert all(value > 0 for value in charges[:5])
    assert charges[5:] == [0.0, 0.0]


def test_tooling_prepaid_is_collected_once_and_not_piece_recovered():
    result = calculated()
    assert row(result, "Y-1")["customer_collections"] == 2500 * 90
    assert all("tooling" not in item["price_trace"]["formula"] for item in result["annual_table"])


def test_negative_cash_has_financing_and_positive_cash_does_not():
    result = calculated()
    yminus = row(result, "Y-1")
    assert yminus["cash_evaluation"] < 0
    assert yminus["financial_charge"] == pytest.approx(abs(yminus["cash_evaluation"]) * 0.08)
    positive = next(item for item in result["annual_table"] if item["cash_evaluation"] > 0)
    assert positive["financial_charge"] == 0


def test_tax_is_nonnegative_and_uses_plant_rate():
    result = calculated()
    for item in result["annual_table"]:
        assert item["taxes"] >= 0
        assert item["taxes"] == pytest.approx(max(0, item["operating_result"]) * 0.25)


def test_cash_flow_reconciliation_and_depreciation_is_non_cash():
    y0 = row(calculated(), "Y0")
    expected = (
        y0["ebitda"] - y0["financial_charge"] - y0["generic_capex"]
        - y0["specific_capex"] - y0["tooling_expenditure"]
        + y0["customer_collections"] - y0["taxes"] - y0["delta_twc"]
        - y0["business_link"]
    )
    assert y0["annual_cash_flow"] == pytest.approx(expected)
    assert "depreciation" not in y0["cash_flow_formula"].lower()


def test_npv_uses_y_minus_1_as_period_zero():
    result = calculated()
    assert row(result, "Y-1")["discount_factor"] == 1
    assert row(result, "Y0")["discount_factor"] == pytest.approx(1 / 1.12)
    assert result["npv"] == pytest.approx(
        sum(item["discounted_cash_flow"] for item in result["annual_table"])
    )


def test_solver_converges_and_reproduces_target():
    inputs = commercial()
    inputs.pop("initial_selling_price")
    inputs["solver_upper_bound"] = 100
    result = solve_selling_price(technical(), inputs, investment_assets=assets())
    assert result["convergence_status"] == "converged"
    assert result["solved_y0_selling_price"] > 0
    assert result["achieved_npv"] == pytest.approx(0, abs=0.00001)
    assert result["commercially_usable"] is False


def test_solver_reports_unbracketed_target():
    inputs = commercial()
    inputs.pop("initial_selling_price")
    inputs["solver_lower_bound"] = 0.000001
    inputs["solver_upper_bound"] = 0.000002
    result = solve_selling_price(technical(), inputs, investment_assets=assets())
    assert result["convergence_status"] == "no_solution_in_bounds"
    assert result["annual_financial_table"] == []


def test_firm_mode_is_blocked_by_unresolved_glue():
    readiness = financial_readiness(technical(), commercial("firm"))
    assert readiness["financial_status"] == "blocked"
    assert "unresolved_component.glue" in readiness["missing_inputs"]


def test_preliminary_mode_excludes_glue_visibly_and_is_not_commercial():
    result = calculated()
    assert result["financial_status"] == "preliminary_incomplete"
    assert result["commercially_usable"] is False
    assert any("glue" in warning for warning in result["warnings"])


def test_decimal_precision_policy_is_exposed():
    result = calculated()
    assert result["rounding_policy"]["calculation_precision"].startswith("Decimal")
    assert Decimal(result["npv_exact"]) == Decimal(str(result["npv_exact"]))

