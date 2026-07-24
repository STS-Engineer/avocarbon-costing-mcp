from decimal import Decimal

import pytest

from services.choke_component_costing import (
    calculate_provisional_glue_consumption,
    resolve_component_ap_terms,
    resolve_ferrite_length_mm,
)
from services.external_component_agent import (
    audit_external_agent_contract_deployment,
    build_agent_prompt,
)
from services import choke_financial_workflow
import json
import shutil
from pathlib import Path
from uuid import uuid4


def test_glue_geometry_uses_eighty_percent_of_ferrite_length():
    result = calculate_provisional_glue_consumption(20)
    assert result["glue_length_mm"] == 16
    assert result["length_factor"] == 0.8
    assert result["status"] == "resolved_assumption"


def test_glue_geometry_uses_one_mm_diameter_and_density_1_5():
    result = calculate_provisional_glue_consumption(20)
    expected_g = (
        Decimal("3.141592653589793")
        * Decimal("0.5")
        * Decimal("0.5")
        * Decimal("16")
        / Decimal("1000")
        * Decimal("1.5")
    )
    assert result["strip_diameter_mm"] == 1
    assert result["density_g_cm3"] == 1.5
    assert result["glue_mass_g_per_product"] == pytest.approx(float(expected_g))
    assert result["glue_mass_kg_per_product"] == pytest.approx(
        float(expected_g / Decimal("1000"))
    )


def test_provisional_glue_is_not_firm_without_approval():
    provisional = calculate_provisional_glue_consumption(20)
    approved = calculate_provisional_glue_consumption(20, approved=True)
    assert provisional["assumption_status"] == "provisional"
    assert provisional["approved"] is False
    assert "validation required" in provisional["warning"].lower()
    assert approved["status"] == "resolved"
    assert approved["approved"] is True
    assert approved["warning"] is None


def test_missing_ferrite_length_does_not_create_glue_quantity():
    result = calculate_provisional_glue_consumption(None)
    assert result["status"] == "blocked"
    assert result["missing_inputs"] == ["confirmed_ferrite_length_mm"]


def test_thirteen_mm_ferrite_uses_one_provisional_strip_with_provenance():
    result = calculate_provisional_glue_consumption(
        13,
        source_field_path="normalized_bom.components[0].length_mm",
        source_evidence=13,
    )
    assert result["glue_length_mm"] == pytest.approx(10.4)
    assert result["glue_mass_kg_per_product"] == pytest.approx(
        0.000012252211349000193
    )
    assert result["application_count"] == 1
    assert result["application_count_status"] == "provisional_one_strip"
    assert result["source_field_path"].startswith("normalized_bom")


def test_ferrite_length_priority_prefers_normalized_dimension():
    normalized = {
        "components": [{
            "component_id": "ferrite_core",
            "length_mm": 13,
        }]
    }
    raw = {
        "bom": [{
            "component_id": "ferrite_core",
            "technical_inputs": {"length_mm": 20},
            "specification": "Ferrite length: 25 mm",
        }]
    }
    result = resolve_ferrite_length_mm(normalized, raw)
    assert result["ferrite_length_mm"] == 13
    assert result["source_priority"] == 1


def test_legacy_ap_aliases_are_normalized_with_provenance():
    result = resolve_component_ap_terms({
        "recommended_offer": {
            "supplier_name": "Supplier A",
            "payment_terms": "Net 60 days",
            "incoterm": "fca",
            "country_of_origin": "India",
            "ap_value_basis": "base_purchase_value",
        }
    })
    assert result["payment_days"] == 60
    assert result["origin_zone"] == "India"
    assert result["source_paths"]["payment_days"] == (
        "recommended_offer.payment_terms"
    )
    assert result["source_paths"]["origin_zone"].startswith(
        "approved_country_to_zone_mapping"
    )


def test_ap_basis_is_never_inferred_from_incoterm():
    result = resolve_component_ap_terms({
        "recommended_offer": {
            "supplier_name": "Supplier A",
            "payment_days": 45,
            "incoterm": "DDP",
            "origin": "India",
        }
    })
    assert result["ap_value_basis"] is None
    assert "ap_value_basis" in result["missing_fields"]


def test_yaml_is_not_claimed_as_deployed_workspace_agent_configuration():
    audit = audit_external_agent_contract_deployment()
    assert audit["yaml_exists"] is True
    assert audit["yaml_loaded_by_local_prompt_builder"] is False
    assert audit["deployment_status"] == "manual_workspace_agent_sync_required"


def test_ap_audit_reads_saved_component_json_and_returns_targeted_rerun(
    monkeypatch,
):
    component_dir = Path(".test-artifacts") / f"ap-audit-{uuid4().hex}"
    component_dir.mkdir(parents=True)
    complete = {
        "recommended_offer": {
            "supplier_name": "Ferrite supplier",
            "payment_terms": "Net 60",
            "incoterm": "FCA",
            "origin": "India",
            "ap_value_basis": "base_purchase_value",
        }
    }
    (component_dir / "ferrite_core.json").write_text(
        json.dumps(complete), encoding="utf-8"
    )
    monkeypatch.setattr(
        choke_financial_workflow,
        "_paths",
        lambda *_: {"components_dir": component_dir},
    )
    monkeypatch.setattr(
        choke_financial_workflow,
        "_commercial_context",
        lambda *_: {},
    )
    monkeypatch.setattr(
        choke_financial_workflow,
        "_technical",
        lambda *_: {
            "component_breakdown": [{
                "component_id": "ferrite_core",
                "material_cost_per_piece": 1.2,
            }]
        },
    )
    try:
        audit = choke_financial_workflow.component_ap_readiness("P", "X")
        ferrite = audit["component_ap_readiness"][0]
        assert ferrite["status"] == "ready"
        assert ferrite["normalized_payment_days"] == 60
        assert audit["components_requiring_rerun"] == [
            "magnet_wire", "lead_tinning", "glue"
        ]
    finally:
        shutil.rmtree(component_dir)


def test_external_component_contract_requires_ap_fields():
    prompt = build_agent_prompt({
        "project_code": "P",
        "product_id": "X",
        "component_id": "core",
        "component_type": "ferrite",
        "component_definition": {"grade": "test"},
        "annual_quantity": 1000,
        "destination_zone": "India",
        "save_address": "data/output.json",
    })
    assert "payment_days" in prompt
    assert "ap_value_basis" in prompt
    assert "origin_zone" in prompt
    assert "base_purchase_value or delivered_purchase_value" in prompt
