import json
from hashlib import sha256
from pathlib import Path

import pytest

from services.choke_excel_golden_reference import (
    extract_golden_reference,
    reconcile_backend_result,
)


FIXTURE = Path(__file__).parent / "fixtures" / "choke_24018_excel_golden_reference.json"
LOCAL_WORKBOOK = Path(
    r"C:\Users\youssef.benamor\Downloads\24018-CHO-00 - 0300440157 - "
    r"PRABHA ENGINEERING- India - Chokes - assy Quotation.xlsm"
)


def reference():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_reviewed_fixture_contains_exact_historical_quotation_results():
    result = reference()
    assert result["source_hash"] == (
        "84b2166bd0a55af2a6b75be09b6fcc67c8cb4a87024b60075fd56d80f7eacbf1"
    )
    assert result["solver"]["solved_part_price"] == pytest.approx(6.1586590903072205)
    assert result["solver"]["quoted_y0_selling_price"] == pytest.approx(7.138881868994289)
    assert result["solver"]["achieved_irr"] == pytest.approx(0.5)
    assert result["solver"]["npv"] == pytest.approx(0, abs=1e-9)
    assert result["manufacturing_cost"]["base_material_per_product"] == pytest.approx(
        1.0657592336443449
    )
    assert result["manufacturing_cost"]["manufacturing_cost_per_product"] == pytest.approx(
        4.857894125363269
    )


def test_historical_component_quantities_are_preserved_not_replaced_by_later_rules():
    components = {
        item["component_id"]: item for item in reference()["component_costs"]
    }
    assert components["glue"]["quantity_per_product"] == pytest.approx(0.00001)
    assert components["glue"]["quantity_unit"] == "Kg"
    assert components["magnet_wire"]["quantity_per_product"] == pytest.approx(0.00091)
    assert components["magnet_wire"]["supplier_unit_price"] == pytest.approx(854.84)
    assert components["lead_tinning"]["historical_tco_per_product"] == pytest.approx(
        0.0262073
    )


def test_reference_exposes_workbook_and_later_model_differences():
    result = reference()
    differences = " ".join(result["business_rule_differences"])
    assert "seven columns" in differences
    assert "50%" in differences and "12%" in differences
    assert "10%" in differences and "8%" in differences
    assert "7 days" in differences and "5 days" in differences


@pytest.mark.skipif(not LOCAL_WORKBOOK.exists(), reason="operator workbook is not installed")
def test_uploaded_workbook_extracts_to_reviewed_reference_without_mutation():
    before = sha256(LOCAL_WORKBOOK.read_bytes()).hexdigest()
    extracted = extract_golden_reference(LOCAL_WORKBOOK)
    after = sha256(LOCAL_WORKBOOK.read_bytes()).hexdigest()
    approved = reference()
    assert before == after == approved["source_hash"]
    assert extracted["manufacturing_cost"] == approved["manufacturing_cost"]
    assert extracted["solver"] == approved["solver"]
    assert extracted["annual_financial_model"] == approved["annual_financial_model"]


def test_reconciliation_uses_reference_as_comparison_not_calculation_input():
    result = reference()
    workbook = result["manufacturing_cost"]
    backend = {
        "material_cost_per_piece": workbook["base_material_per_product"],
        "transport_cost_per_piece": workbook["inbound_transport_per_product"],
        "delivered_material_cost_per_piece": workbook["delivered_material_per_product"],
        "dl_cost_per_piece": workbook["dl_per_product"],
        "voh_cost_per_piece": workbook["voh_per_product"],
        "direct_cost_per_piece": workbook["direct_cost_per_product"],
        "foh_cost_per_piece": workbook["foh_per_product"],
        "fee_cost_per_piece": workbook["fees_per_product"],
        "manufacturing_cost_per_piece": workbook["manufacturing_cost_per_product"],
    }
    comparison = reconcile_backend_result(result, backend)
    assert comparison["status"] == "match"
    assert comparison["historical_values_used_in_calculation"] is False
    assert all(row["status"] == "match" for row in comparison["rows"])


def test_reconciliation_reports_material_difference_without_broad_rounding():
    result = reference()
    comparison = reconcile_backend_result(result, {"material_cost_per_piece": 1})
    material = next(
        row for row in comparison["rows"]
        if row["backend_field"] == "material_cost_per_piece"
    )
    assert material["status"] == "mismatch"
    assert material["difference"] == pytest.approx(
        1 - result["manufacturing_cost"]["base_material_per_product"]
    )
