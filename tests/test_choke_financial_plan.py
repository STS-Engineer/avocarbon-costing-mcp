from decimal import Decimal

import pytest

from services.choke_financial_plan import (
    build_historical_comparison,
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
            "origin_zone": "Europe",
            "ap_value_basis": (
                "delivered_purchase_value"
                if component == "magnet_wire"
                else "base_purchase_value"
            ),
            "supplier": f"{component} supplier",
            "source_paths": {
                "payment_days": f"recommended_offer.{component}.payment_days",
                "ap_value_basis": f"recommended_offer.{component}.ap_value_basis",
                "incoterm": f"recommended_offer.{component}.incoterm",
                "origin_zone": f"recommended_offer.{component}.origin_zone",
            },
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
        "financing_interest_basis": "closing_balance",
        "wip_material_basis": "base_material",
        "profitability_target": {"type": "npv_zero", "value": 0},
        "product_profitability_target": {
            "source_field": "products.roce_target_percent",
            "value": 30,
            "target_interpretation": "npv_zero",
        },
        "capex_tooling_treatment": {
            "generic_capex": {"type": "avocarbon_owned"},
            "specific_capex": {"type": "avocarbon_owned"},
            "tooling": {"type": "prepaid"},
        },
        "investment_fx_rates": {"EUR": 90},
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
    assert {
        item["ap_value_basis"] for item in y0["ap_component_breakdown"]
    } == {"base_purchase_value", "delivered_purchase_value"}


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
    assert row(result, "Y0")["depreciation"] == 0
    charges = [row(result, f"Y{year}")["depreciation"] for year in range(1, 7)]
    assert all(value > 0 for value in charges[:5])
    assert charges[5] == 0


def test_tooling_prepaid_is_collected_once_and_not_piece_recovered():
    result = calculated()
    assert row(result, "Y-1")["customer_collections"] == 2500 * 90
    assert all("tooling" not in item["price_trace"]["formula"] for item in result["annual_table"])


def test_financing_carries_forward_and_positive_cash_repays_balance():
    result = calculated()
    yminus = row(result, "Y-1")
    assert yminus["cash_evaluation"] < 0
    assert yminus["financial_charge"] == pytest.approx(abs(yminus["cash_evaluation"]) * 0.08)
    y0 = row(result, "Y0")
    assert y0["opening_financing_balance"] == pytest.approx(
        yminus["closing_financing_balance"]
    )
    repayment_year = next(
        item for item in result["annual_table"]
        if item["financing_repayment"] > 0
    )
    assert repayment_year["applicable_financing_balance"] == pytest.approx(
        repayment_year["opening_financing_balance"]
        + repayment_year["financing_drawdown"]
        - repayment_year["financing_repayment"]
    )


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


def test_pure_service_is_deterministic_for_identical_inputs():
    first = calculated()
    second = calculated()
    assert first == second
    assert "calculated_at" not in first


def test_components_use_their_own_ap_purchase_basis():
    result = calculated()
    traces = {
        item["component_id"]: item
        for item in row(result, "Y0")["ap_component_breakdown"]
    }
    ferrite = traces["ferrite_core"]
    wire = traces["magnet_wire"]
    assert ferrite["ap_value_basis"] == "base_purchase_value"
    assert wire["ap_value_basis"] == "delivered_purchase_value"
    assert ferrite["annual_purchase_value"] == pytest.approx(
        360000 * 2.274288
    )
    assert wire["annual_purchase_value"] == pytest.approx(
        360000 * 1.364940
    )
    assert ferrite["source_paths"]["ap_value_basis"]


def test_missing_component_ap_basis_warns_preliminary_and_blocks_firm():
    preliminary = commercial("preliminary")
    preliminary["supplier_terms"]["ferrite_core"].pop("ap_value_basis")
    result = calculate_financial_plan(technical(), preliminary, investment_assets=[])
    assert result["financial_preliminary_status"] == "preliminary_assumption"
    ferrite = next(
        item for item in row(result, "Y0")["ap_component_breakdown"]
        if item["component_id"] == "ferrite_core"
    )
    assert ferrite["status"] == "excluded_preliminary"

    firm = commercial("firm")
    firm["supplier_terms"]["ferrite_core"].pop("ap_value_basis")
    readiness = financial_readiness(technical(False), firm)
    assert readiness["financial_firm_status"] == "blocked"
    assert "component_ap.ferrite_core.ap_value_basis" in readiness[
        "financial_firm_blockers"
    ]


def test_wip_uses_material_plus_half_dl_voh_foh_and_excludes_fee():
    y0 = row(calculated(), "Y0")
    trace = y0["inventory_trace"]["wip"]
    expected_basis = (
        y0["per_product"]["base_material"]
        + (
            y0["per_product"]["dl"]
            + y0["per_product"]["voh"]
            + y0["per_product"]["foh"]
        ) / 2
    )
    assert trace["basis_per_product"] == pytest.approx(expected_basis)
    assert y0["wip"] == pytest.approx(
        y0["quantity"] / 365 * trace["days"] * expected_basis
    )
    assert "Fee" not in trace["formula"]


def test_financing_balance_reconciles_y_minus_1_through_y6():
    result = calculated()
    previous_closing = 0
    for annual in result["annual_table"]:
        assert annual["opening_financing_balance"] == pytest.approx(
            previous_closing
        )
        assert annual["applicable_financing_balance"] == pytest.approx(
            annual["opening_financing_balance"]
            + annual["financing_drawdown"]
            - annual["financing_repayment"]
        )
        assert annual["financial_charge"] == pytest.approx(
            annual["applicable_financing_balance"] * 0.08
        )
        assert annual["closing_financing_balance"] == pytest.approx(
            annual["applicable_financing_balance"]
            + annual["financial_charge"]
        )
        previous_closing = annual["closing_financing_balance"]


def test_each_depreciable_asset_has_exact_y1_to_y5_charges():
    result = calculated()
    for asset in result["investment_schedule"]["assets"]:
        if not asset["depreciable_basis"]:
            continue
        charged = [
            item["period"] for item in asset["depreciation_schedule"]
            if item["charge"] > 0
        ]
        assert charged == ["Y1", "Y2", "Y3", "Y4", "Y5"]
        assert asset["depreciation_start_period"] == "Y1"
        assert asset["depreciation_end_period"] == "Y5"


def test_solver_reports_product_target_source_and_fixed_discount_rate():
    inputs = commercial()
    inputs.pop("initial_selling_price")
    inputs["discount_rate"] = 4
    inputs["solver_upper_bound"] = 100
    result = solve_selling_price(technical(), inputs, investment_assets=assets())
    assert result["convergence_status"] == "converged"
    assert result["source_product_target_field"] == "products.roce_target_percent"
    assert result["target_interpretation"] == "npv_zero"
    assert result["discount_rate"] == 0.12
    assert result["residual"] == pytest.approx(0, abs=0.00001)


def test_ambiguous_product_roce_semantics_block_solver():
    inputs = commercial()
    inputs.pop("initial_selling_price")
    inputs["product_profitability_target"] = {
        "source_field": "products.roce_target_percent",
        "value": 30,
        "target_interpretation": None,
        "blocking_business_decision": "Confirm ROCE to NPV semantics.",
    }
    result = solve_selling_price(technical(), inputs, investment_assets=[])
    assert result["convergence_status"] == "blocked"
    assert result["source_product_target_field"] == (
        "products.roce_target_percent"
    )
    assert "Confirm ROCE" in result["message"]
    assert result["business_blocker"] == {
        "code": "roce_to_npv_semantics_unconfirmed",
        "source_field": "products.roce_target_percent",
        "source_value": 30,
        "discount_rate_percent": 12,
    }


def test_npv_zero_solver_is_explicitly_scenario_only():
    inputs = commercial()
    inputs.pop("initial_selling_price")
    inputs["scenario_solver"] = True
    inputs["solver_upper_bound"] = 100
    inputs["product_profitability_target"]["target_interpretation"] = None
    result = solve_selling_price(technical(), inputs, investment_assets=assets())
    assert result["convergence_status"] == "converged"
    assert result["solver_type"] == "scenario_solver"
    assert result["commercially_usable"] is False


def test_ar_trace_records_explicit_source_and_formula():
    inputs = commercial()
    inputs["customer_payment_term"] = "Net 45"
    inputs["customer_payment_days_source"] = "contract_mapping:net_45"
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    trace = row(result, "Y0")["ar_trace"]
    assert trace["source_payment_term"] == "Net 45"
    assert trace["normalized_payment_days"] == 45
    assert trace["normalization_source"] == "contract_mapping:net_45"
    assert trace["value"] == row(result, "Y0")["ar"]


def test_inventory_buckets_expose_basis_formula_and_source():
    result = calculated()
    trace = row(result, "Y0")["inventory_trace"]
    assert set(trace) == {
        "rm_transit", "rm_in_house", "wip", "fg_in_house",
        "fg_transit", "fg_platform",
    }
    assert all(item["formula"] and item["source"] for item in trace.values())
    assert trace["wip"]["value_basis"] == "base_material_plus_half_conversion"
    assert trace["wip"]["fee_per_product_excluded"] > 0


def test_platform_stock_rules_move_days_to_transit_and_platform():
    inputs = commercial()
    inputs.update({
        "platform": True,
        "customer_transit_days": 12,
        "platform_safety_stock_days": 8,
    })
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    days = row(result, "Y0")["stock_days"]
    assert days["fg_in_house"] == pytest.approx(2 / 3 * 7)
    assert days["fg_transit"] == 12
    assert days["fg_platform"] == pytest.approx(8 + 2 / 3 * 7)


def test_same_zone_raw_material_defaults_are_traced():
    inputs = commercial()
    for value in inputs["supplier_terms"].values():
        value["zone_relation"] = "same"
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    component = row(result, "Y0")["ap_component_breakdown"][0]
    assert component["rm_transit_days"] == 7
    assert component["rm_in_house_days"] == pytest.approx(
        0.2 * 7 + 0.2 * 7 + 2 / 3 * 7
    )


def test_depreciation_trace_reconciles_book_values():
    result = calculated()
    yminus = row(result, "Y-1")["depreciation_trace"]
    assert yminus["beginning_book_value"] == 0
    assert yminus["ending_book_value"] > 0
    prior = yminus["ending_book_value"]
    for period in ("Y0", "Y1", "Y2", "Y3", "Y4", "Y5", "Y6"):
        trace = row(result, period)["depreciation_trace"]
        assert trace["beginning_book_value"] == pytest.approx(prior)
        assert trace["ending_book_value"] == pytest.approx(
            max(0, trace["beginning_book_value"] - trace["charge"])
        )
        prior = trace["ending_book_value"]


def test_wip_delivered_material_does_not_add_transport_twice():
    inputs = commercial()
    inputs["wip_material_basis"] = "delivered_material"
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    y0 = row(result, "Y0")
    trace = y0["inventory_trace"]["wip"]
    expected_material = (
        y0["per_product"]["base_material"] + y0["per_product"]["transport"]
    )
    assert trace["material_per_product"] == pytest.approx(expected_material)
    assert trace["basis_per_product"] == pytest.approx(
        expected_material
        + (
            y0["per_product"]["dl"]
            + y0["per_product"]["voh"]
            + y0["per_product"]["foh"]
        ) / 2
    )
    assert "transport" not in trace["formula"].lower()


@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        ("closing_balance", lambda annual: annual["applicable_financing_balance"]),
        ("opening_balance", lambda annual: annual["opening_financing_balance"]),
        (
            "average_balance",
            lambda annual: (
                annual["opening_financing_balance"]
                + annual["applicable_financing_balance"]
            ) / 2,
        ),
    ],
)
def test_financing_interest_timing_policies(basis, expected):
    inputs = commercial()
    inputs["financing_interest_basis"] = basis
    result = calculate_financial_plan(technical(), inputs, investment_assets=[])
    assert result["financing_interest_basis"] == basis
    for annual in result["annual_table"]:
        assert annual["financing_interest_basis_value"] == pytest.approx(
            expected(annual), abs=0.000001
        )
        assert annual["financial_charge"] == pytest.approx(
            expected(annual) * 0.08, abs=0.000001
        )


def test_excluded_investment_is_preserved_but_not_scheduled():
    excluded = [{
        "source_id": "wp_unapproved",
        "category": "generic_capex",
        "amount": 999,
        "currency": "EUR",
        "included": False,
        "exclusion_reason": "not approved",
    }]
    result = calculate_financial_plan(technical(), commercial(), investment_assets=excluded)
    assert row(result, "Y-1")["generic_capex"] == 0
    detail = result["investment_schedule"]["assets"][0]
    assert detail["included"] is False
    assert detail["exclusion_reason"] == "not approved"


@pytest.mark.parametrize("reference_name", ["current_rod_choke", "completed_assembly_rfq"])
def test_historical_comparison_is_independent_validation(reference_name):
    report = build_historical_comparison(
        {"Y0.selling_price": 20, "Y0.ebitda": 100},
        {"Y0.selling_price": 21, "Y0.ebitda": 90},
        {"Y0.selling_price": reference_name},
        {"Y0.selling_price": False},
        "Olivier",
    )
    assert report["historical_values_used_in_calculation"] is False
    assert report["status"] == "comparison_only"
    price = next(item for item in report["rows"] if item["metric"] == "Y0.selling_price")
    assert price["absolute_difference"] == 1
    assert price["accepted"] is False
    assert price["validation_owner"] == "Olivier"
