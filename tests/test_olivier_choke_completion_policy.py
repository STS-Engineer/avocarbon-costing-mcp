from services.choke_financial_plan import _technical_costs
from services.choke_financial_workflow import (
    _apply_olivier_choke_policy,
    _component_zone_relation,
)
from services.choke_sequential_agent_workflow import _external_costing_route


def test_tin_consumable_routes_to_external_material_despite_operation_label():
    component = {
        "component_id": "lead_tinning",
        "component": "Tin finishing consumable",
        "costing_route": "not_external_agent",
        "category": "tinning",
    }
    assert _external_costing_route(component) == "tin"


def test_complete_choke_is_never_routed_as_external_component():
    component = {
        "component_id": "complete_product",
        "component": "Complete Rod Choke assembly",
    }
    assert _external_costing_route(component) is None


def test_preliminary_costs_use_resolved_component_subtotals_only_when_allowed():
    technical = {
        "base_material_cost_per_piece": None,
        "material_cost_per_piece": None,
        "logistics_cost_per_piece": None,
        "transport_cost_per_piece": None,
        "delivered_material_cost_per_piece": None,
        "calculated_material_cost_for_resolved_components": 2.5,
        "calculated_transport_cost_for_resolved_components": 0.4,
        "calculated_delivered_material_cost_for_resolved_components": 2.9,
        "dl_cost_per_piece": 1,
        "voh_cost_per_piece": 0.5,
        "foh_percent_dc": 10,
        "fee_percent_dc": 5,
    }
    firm = _technical_costs(technical, allow_resolved_subtotals=False)
    preliminary = _technical_costs(technical, allow_resolved_subtotals=True)
    assert float(firm["base_material"]) == 0
    assert float(firm["logistics"]) == 0
    assert float(preliminary["base_material"]) == 2.5
    assert float(preliminary["logistics"]) == 0.4
    assert float(preliminary["delivered_material"]) == 2.9


def test_olivier_policy_creates_standard_choke_profile_and_npv_target():
    commercial = {
        "annual_quantity": 1000,
        "sop_date": "2027-06-01",
        "currency": "EUR",
        "product": "Rod Choke",
    }
    result = _apply_olivier_choke_policy(commercial, {}, {})
    assert result["sop_year"] == 2027
    assert result["annual_quantities"] == {
        "Y-1": 0,
        "Y0": 500,
        "Y1": 1000,
        "Y2": 1000,
        "Y3": 1000,
        "Y4": 1000,
        "Y5": 500,
        "Y6": 0,
    }
    assert result["customer_productivity"] == {
        "percentage": 3,
        "start_year": 1,
        "duration": 3,
        "basis": "added_value",
    }
    assert result["discount_rate"] == 12
    assert result["solver_discount_rate"] == 12
    assert result["financing_rate"] == 8
    assert result["product_profitability_target"]["target_interpretation"] == "npv_zero"


def test_policy_never_invents_missing_sop():
    result = _apply_olivier_choke_policy(
        {"annual_quantity": 1000, "currency": "EUR"}, {}, {}
    )
    assert "sop_year" not in result


def test_project_specific_values_override_policy():
    explicit = {
        "customer_payment_days": 45,
        "customer_incoterm": "DAP",
        "annual_quantities": {
            "Y-1": 0, "Y0": 100, "Y1": 200, "Y2": 300,
            "Y3": 300, "Y4": 200, "Y5": 100, "Y6": 0,
        },
    }
    result = _apply_olivier_choke_policy(
        {"annual_quantity": 1000, "sop_year": 2027, **explicit}, {}, explicit
    )
    assert result["customer_payment_days"] == 45
    assert result["customer_incoterm"] == "DAP"
    assert result["annual_quantities"] == explicit["annual_quantities"]


def test_component_zone_relation_is_derived_from_origin_and_production():
    commercial = {
        "production_plant": "Chennai",
        "customer_delivery_zone": "India",
    }
    assert _component_zone_relation("India", commercial) == "same"
    assert _component_zone_relation("China", commercial) == "different"
