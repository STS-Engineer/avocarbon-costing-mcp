import math
from decimal import Decimal

from services import choke_component_costing as costing
from services import choke_sequential_agent_workflow as workflow


def _live_bom():
    return {
        "bill_of_material": [
            {
                "component_id": "ferrite_core",
                "poste": "Ferrite",
                "produit_designation": "Ferrite core rod",
                "quantite": "2 pcs",
                "poids_par_piece": "A confirmer",
            },
            {
                "component_id": "magnet_wire",
                "poste": "Fil",
                "produit_designation": "Copper wire AIEW",
                "quantite": "1 bobinage / piece",
                "poids_par_piece": "0.779 g estime",
            },
            {
                "component_id": "lead_tinning",
                "poste": "Etamage",
                "produit_designation": "Tin coating",
                "quantite": "2 zones potentielles",
                "poids_par_piece": "A confirmer; indicatif 0.00424 g pour 12 mm total",
            },
            {
                "component_id": "glue",
                "poste": "Colle",
                "produit_designation": "EP-138 epoxy resin",
                "quantite": "A confirmer",
                "poids_par_piece": (
                    "A confirmer; indicatif 0.01634 g pour 1 bande "
                    "ou 0.03267 g pour 2 bandes"
                ),
            },
        ]
    }


def _offer(
    component_id,
    unit_price,
    currency,
    pricing_unit,
    delivered,
    *,
    converted_unit_price=None,
    converted_currency=None,
    original_currency=None,
    conversion_rate=None,
    technical_specification=None,
    transport=0,
    customs=0,
    forwarder=0,
    capital=0,
    unit_price_basis=None,
):
    audit_currency = converted_currency or currency
    return {
        "component_id": component_id,
        "classification": "External",
        "commercially_usable": False,
        "technical_specification": technical_specification or {},
        "recommended_offer": {
            "unit_price": unit_price,
            "unit_price_currency": currency,
            "currency": currency,
            "pricing_unit": pricing_unit,
            "unit_price_basis": (
                unit_price_basis or f"{currency}/{pricing_unit}"
            ),
            "converted_unit_price": converted_unit_price,
            "converted_currency": converted_currency,
            "original_currency": original_currency,
            "conversion_rate": conversion_rate,
            "transport_cost": transport,
            "transport_basis": f"{audit_currency}/{pricing_unit}",
            "customs_cost": customs,
            "customs_basis": f"{audit_currency}/{pricing_unit}",
            "forwarder_fee": forwarder,
            "forwarder_basis": f"{audit_currency}/{pricing_unit}",
            "supply_chain": {
                "delivered_cost": delivered,
                "delivered_cost_currency": f"{audit_currency}/{pricing_unit}",
                "transportation_cost": transport,
                "custom_duty_cost": customs,
                "forwarder_cost": forwarder,
                "capital_cost_12pct": capital,
                "capital_cost_basis": f"{audit_currency}/{pricing_unit}",
                "currency": audit_currency,
            },
        },
    }


def _live_outputs():
    return [
        {
            "component_id": "ferrite_core",
            "agent_raw_output": _offer(
                "ferrite_core",
                0.08,
                "CNY",
                "pc",
                1.369142,
                converted_unit_price=1.137144,
                converted_currency="INR",
                original_currency="CNY",
                conversion_rate=14.2143,
                transport=0.09,
                customs=0.092036,
                forwarder=0.03,
                capital=0.019962,
            ),
        },
        {
            "component_id": "magnet_wire",
            "agent_raw_output": _offer(
                "magnet_wire",
                1720,
                "INR",
                "kg",
                1752.17,
                transport=18,
                forwarder=5,
                capital=9.17,
                unit_price_basis=(
                    "INR per kg of raw enameled wire, excluding winding and "
                    "internal added value"
                ),
                technical_specification={
                    "estimated_mass_per_piece_g": 0.779,
                    "estimated_engaged_length_mm": 196.824,
                    "diameter_mm": 0.75,
                },
            ),
        },
        {
            "component_id": "lead_tinning",
            "agent_raw_output": _offer(
                "lead_tinning",
                6233.204589718751,
                "INR",
                "kg",
                6857.285819265275,
                converted_unit_price=4986.563671775,
                converted_currency="INR",
                original_currency="USD",
                conversion_rate=95.366395,
                transport=524.11,
                capital=99.971229546524,
                technical_specification={
                    "indicative_tin_mass_per_product_g": 0.00424,
                    "indicative_tin_mass_per_product_kg": 0.00000424,
                },
            ),
        },
        {
            "component_id": "glue",
            "agent_raw_output": _offer(
                "glue",
                10857,
                "INR",
                "kg",
                11115.57,
                transport=150,
                capital=108.57,
                technical_specification={
                    "confirmed_mass_per_piece_g": None,
                    "indicative_mass_per_piece_g": {
                        "one_band": 0.01634,
                        "two_bands": 0.03267,
                    },
                },
            ),
        },
    ]


def _unit_data():
    return {
        "status": "found",
        "plant": "Chennai",
        "operating_currency": "INR",
        "selling_currency": "INR",
        "dl_rate_operating_per_hour": 220,
        "voh_rate_operating_per_hour": 110,
        "open_hours_per_year": 4752,
        "foh_percent_dc": 132,
        "fee_percent_dc": 70,
    }


def _run_final(monkeypatch, result_mode):
    unit = _unit_data()
    monkeypatch.setattr(
        workflow,
        "_load_state",
        lambda *_: {
            "customer_input": {"currency": "INR", "annual_quantity": 360000},
            "unit_data": unit,
        },
    )
    monkeypatch.setattr(workflow, "_read_json", lambda *_args, **_kwargs: _live_bom())
    monkeypatch.setattr(workflow, "_load_saved_component_outputs", lambda *_: _live_outputs())
    monkeypatch.setattr(
        workflow,
        "_load_saved_most_outputs",
        lambda *_: [
            {
                "work_package_id": "wp_10_wire_winding",
                "p_h": 288,
                "oee": 0.8,
                "operator_percent": 100,
            }
        ],
    )
    monkeypatch.setattr(workflow, "_write_json", lambda path, _value: str(path))
    return workflow.calculate_final_choke_costing_from_saved_outputs(
        "24018-CHO-00",
        "300440157",
        unit_data_override=unit,
        result_mode=result_mode,
    )


def test_converted_ferrite_price_is_used_without_second_fx_lookup():
    raw = _live_outputs()[0]["agent_raw_output"]
    price = costing.resolve_unit_price(raw, target_currency="INR")
    assert price["unit_price"] == 1.137144
    assert price["unit_price_currency"] == "INR"
    assert price["fx"]["status"] == "already_converted"
    assert price["fx"]["rate"] == 14.2143


def test_live_quantity_resolution_for_ferrite_wire_tin_and_glue():
    fields = workflow._saved_bom_dimensional_map(_live_bom())
    outputs = {
        item["component_id"]: item["agent_raw_output"] for item in _live_outputs()
    }
    ferrite = costing.resolve_component_pricing_quantity(
        "ferrite_core", "ferrite", fields["ferrite_core"], outputs["ferrite_core"]
    )
    wire = costing.resolve_component_pricing_quantity(
        "magnet_wire", "wire", fields["magnet_wire"], outputs["magnet_wire"]
    )
    tin = costing.resolve_component_pricing_quantity(
        "lead_tinning", "tin", fields["lead_tinning"], outputs["lead_tinning"]
    )
    glue = costing.resolve_component_pricing_quantity(
        "glue", "glue", fields["glue"], outputs["glue"]
    )
    assert ferrite == {
        "pricing_quantity": 2.0,
        "pricing_unit": "pc",
        "pricing_quantity_basis": "bom_count",
        "technical_length_m_per_product": None,
        "technical_mass_kg_per_product": None,
    }
    assert math.isclose(wire["pricing_quantity"], 0.000779, rel_tol=1e-12)
    assert wire["pricing_quantity_basis"] == "estimated_bom_mass"
    assert math.isclose(tin["pricing_quantity"], 0.00000424, rel_tol=1e-12)
    assert tin["pricing_quantity_basis"] == "indicative_conditional"
    assert glue["pricing_quantity"] is None


def test_delivered_cost_uses_offer_currency_inheritance_and_is_not_double_counted():
    raw = _live_outputs()[1]["agent_raw_output"]
    delivered = costing.resolve_delivered_unit_cost(raw, "INR")
    assert delivered["status"] == "calculated"
    assert delivered["delivered_cost_per_pricing_unit"] == 1752.17
    assert delivered["reported_delivered_cost_used"] is True
    assert delivered["calculated_delivered_unit_cost"] == 1752.17
    assert delivered["reconciliation_difference"] == 0


def test_currency_less_root_logistics_is_excluded_and_reported():
    result = costing.compute_component_transport_cost(
        "magnet_wire",
        {
            "recommended_offer": {
                "currency": "INR",
                "pricing_unit": "kg",
            },
            "transport_cost": 10,
            "transport_basis": "INR/kg",
        },
        {"pricing_quantity": 0.001, "pricing_unit": "kg"},
        target_currency="INR",
        exclude_invalid=True,
    )
    assert result["status"] == "calculated_with_exclusions"
    assert result["excluded_adders"][0]["reason"] == "currency_missing"


def test_firm_result_keeps_resolved_material_subtotal_but_blocks_for_glue(monkeypatch):
    result = _run_final(monkeypatch, "firm")
    rows = {item["component_id"]: item for item in result["component_breakdown"]}
    assert result["status"] == "blocked"
    assert result["material_cost_per_piece"] > 0
    assert result["direct_cost_per_piece"] is None
    assert rows["ferrite_core"]["fx"]["status"] == "already_converted"
    assert math.isclose(rows["ferrite_core"]["material_cost_per_piece"], 2 * 1.137144)
    assert math.isclose(rows["magnet_wire"]["technical_quantity"], 0.000779)
    assert math.isclose(rows["lead_tinning"]["technical_quantity"], 0.00000424)
    assert rows["glue"]["blocking_reason"] == "technical_quantity_unit_unknown"
    assert result["unresolved_material_components"][0]["message"] == (
        "Glue consumption per product required."
    )


def test_preliminary_result_calculates_resolved_subtotal_and_labels_incomplete(monkeypatch):
    result = _run_final(monkeypatch, "preliminary")
    assert result["status"] == "preliminary_incomplete"
    assert result["material_completeness"] == {
        "resolved_component_count": 3,
        "total_component_count": 4,
        "unresolved_component_count": 1,
        "percentage": 75.0,
    }
    assert result["calculated_material_cost_for_resolved_components"] > 0
    assert result["calculated_delivered_material_cost_for_resolved_components"] > 0
    assert result["direct_cost_per_piece"] is not None
    assert result["foh_cost_per_piece"] is not None
    assert result["fee_cost_per_piece"] is not None
    assert result["commercially_usable"] is False
    assert any("not quotation-ready" in item for item in result["warnings"])
    assert result["material_completeness"]["resolved_component_count"] == 3
    assert result["material_completeness"]["unresolved_component_count"] == 1
    assert result["material_completeness"]["percentage"] == 75.0
    assert result["unresolved_material_components"] == [{
        "component_id": "glue",
        "reason": "technical_quantity_unit_unknown",
        "message": "Glue consumption per product required.",
    }]


def test_descriptive_wire_basis_uses_canonical_normalized_pricing_unit(monkeypatch):
    result = _run_final(monkeypatch, "preliminary")
    wire = next(
        item for item in result["component_breakdown"]
        if item["component_id"] == "magnet_wire"
    )
    assert wire["pricing_unit"] == "kg"
    assert wire["status"] == "resolved"
    assert wire["blocking_reason"] is None
    assert wire["material_cost_per_piece"] == 1.33988
    assert wire["delivered_material_cost_per_piece"] == 1.36494043
    assert wire["warnings"] == [
        "Final wire cut length and mass remain unconfirmed."
    ]


def test_subtotals_and_logistics_use_exactly_the_resolved_component_set(monkeypatch):
    result = _run_final(monkeypatch, "preliminary")
    resolved = [
        item for item in result["component_breakdown"]
        if item["status"] == "resolved"
    ]
    assert {item["component_id"] for item in resolved} == {
        "ferrite_core", "magnet_wire", "lead_tinning",
    }
    base = sum(
        Decimal(item["material_cost_per_piece_exact"]) for item in resolved
    )
    delivered = sum(
        Decimal(item["delivered_material_cost_per_piece_exact"])
        for item in resolved
    )
    assert Decimal(result["calculated_material_cost_exact"]) == base
    assert Decimal(result["calculated_delivered_material_cost_exact"]) == delivered
    assert Decimal(result["transport_cost_per_piece_exact"]) == delivered - base
    assert result["material_cost_per_piece"] == float(base)
    assert result["delivered_material_cost_per_piece"] == float(delivered)
    assert result["transport_cost_per_piece"] == float(delivered - base)
    for item in result["transport_breakdown_by_component"]:
        if item["component_id"] != "glue":
            assert item["status"] == "calculated"
            assert item["excluded_adders"] == []


def test_firm_mode_is_blocked_only_by_glue_and_has_no_final_foh_fee(monkeypatch):
    result = _run_final(monkeypatch, "firm")
    component_missing = [
        item for item in result["missing_inputs"]
        if item.startswith("component_outputs:")
    ]
    assert component_missing == [
        "component_outputs:glue:technical_quantity_unit_unknown"
    ]
    assert result["status"] == "blocked"
    assert result["commercially_usable"] is False
    assert result["direct_cost_per_piece"] is None
    assert result["foh_cost_per_piece"] is None
    assert result["fee_cost_per_piece"] is None
    assert result["manufacturing_cost_per_piece"] is None


def test_all_live_delivered_costs_have_traceable_decimal_formulas():
    for item in _live_outputs():
        raw = item["agent_raw_output"]
        audit = costing.reconcile_delivered_unit_cost(raw, "INR")
        assert audit["status"] == "calculated"
        assert audit["reconciliation_difference"] == 0
        assert audit["reported_delivered_cost_used"] is True
        assert audit["delivered_cost_formula"].endswith(
            str(audit["calculated_delivered_unit_cost"])
        )
        assert audit["rounding_policy"]["intermediate_rounding"] == "none"
        assert audit["rounding_policy"]["decimal_context_precision"] == 28


def test_tin_same_currency_offer_price_keeps_small_lot_premium():
    raw = _live_outputs()[2]["agent_raw_output"]
    raw["recommended_offer"]["small_lot_and_distribution_premium_percent"] = 25
    price = costing.resolve_unit_price(raw, target_currency="INR")
    assert price["unit_price"] == 6233.204589718751
    assert price["unit_price_currency"] == "INR"
    assert price["fx"] is None
    audit = costing.reconcile_delivered_unit_cost(raw, "INR")
    assert audit["base_unit_cost"] == 6233.204589718751
    assert audit["base_cost_adjustments"][0]["percent"] == 25


def test_reconciliation_difference_blocks_delivered_cost():
    raw = _live_outputs()[1]["agent_raw_output"]
    raw["recommended_offer"]["supply_chain"]["delivered_cost"] = 1800
    audit = costing.reconcile_delivered_unit_cost(raw, "INR")
    assert audit["status"] == "blocked"
    assert audit["reason"] == "delivered_cost_reconciliation_mismatch"
    assert audit["delivered_cost_per_pricing_unit"] is None


def test_currency_less_root_adder_is_excluded_from_delivered_reconciliation():
    raw = {
        "transport_cost": 5,
        "transport_basis": "INR/kg",
        "recommended_offer": {
            "unit_price": 100,
            "currency": "INR",
            "pricing_unit": "kg",
            "delivered_cost": 105,
            "delivered_cost_currency": "INR",
            "delivered_cost_basis": "INR/kg",
        },
    }
    audit = costing.reconcile_delivered_unit_cost(raw, "INR")
    assert audit["status"] == "blocked"
    assert audit["reason"] == "delivered_cost_reconciliation_mismatch"
    assert audit["excluded_adders"][0]["reason"] == "currency_missing"


def test_final_result_blocks_when_one_delivered_total_does_not_reconcile(monkeypatch):
    outputs = _live_outputs()
    outputs[1]["agent_raw_output"]["recommended_offer"]["supply_chain"][
        "delivered_cost"
    ] = 1800
    unit = _unit_data()
    monkeypatch.setattr(
        workflow,
        "_load_state",
        lambda *_: {
            "customer_input": {"currency": "INR", "annual_quantity": 360000},
            "unit_data": unit,
        },
    )
    monkeypatch.setattr(workflow, "_read_json", lambda *_args, **_kwargs: _live_bom())
    monkeypatch.setattr(workflow, "_load_saved_component_outputs", lambda *_: outputs)
    monkeypatch.setattr(
        workflow,
        "_load_saved_most_outputs",
        lambda *_: [{
            "work_package_id": "wp_10_wire_winding",
            "p_h": 288,
            "oee": 0.8,
            "operator_percent": 100,
        }],
    )
    monkeypatch.setattr(workflow, "_write_json", lambda path, _value: str(path))
    result = workflow.calculate_final_choke_costing_from_saved_outputs(
        "24018-CHO-00",
        "300440157",
        unit_data_override=unit,
        result_mode="preliminary",
    )
    wire = next(
        item for item in result["component_breakdown"]
        if item["component_id"] == "magnet_wire"
    )
    assert result["status"] == "preliminary_incomplete"
    assert wire["status"] == "blocked"
    assert wire["blocking_reason"] == "delivered_cost_reconciliation_mismatch"
    assert result["direct_cost_per_piece"] is None


def test_decimal_material_subtotal_matches_reconciled_live_values(monkeypatch):
    result = _run_final(monkeypatch, "preliminary")
    expected = (
        Decimal("2") * Decimal("1.369142")
        + Decimal("0.000779") * Decimal("1752.17")
        + Decimal("0.00000424") * Decimal("6857.285819265275")
    )
    assert Decimal(result["calculated_delivered_material_cost_exact"]) == expected
    assert round(
        result["calculated_delivered_material_cost_for_resolved_components"], 6
    ) == 4.132299


def test_external_material_classification_is_not_changed_by_most_participation():
    normalized = workflow.normalize_bom(_live_bom())
    process = workflow.build_most_process_decomposition(
        {
            "project_code": "P",
            "product_id": "X",
            "customer_input": {"product": "Rod Choke"},
        },
        normalized,
    )
    report = workflow._most_eligibility_report(normalized, process)
    rows = {item["component_id"]: item for item in report["components"]}
    assert rows["ferrite_core"]["external_or_internal"] == "external"
    assert rows["magnet_wire"]["external_or_internal"] == "external"
    assert rows["lead_tinning"]["external_or_internal"] == "external"
