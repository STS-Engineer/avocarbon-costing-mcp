from pathlib import Path

import openpyxl

from app.routers.choke_costing_ui_router import _unique_upload_path
from services.customer_input_extraction import (
    apply_resolution_to_customer_input,
    discover_customer_input_files,
    extract_customer_input_package,
    extract_excel_fields,
    merge_customer_input_candidates,
    normalize_delivery_location,
)
from services.customer_input_schema import normalize_customer_input


def _workbook(path: Path, *, hidden_currency=None, layout="rows") -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "RFQ"
    values = {
        "Project code": "24018-CHO-00",
        "Customer": "PRABHA ENGINEERING",
        "Final customer": "Prabha Engineering",
        "Product": "Rod Choke",
        "Product line": "Choke",
        "Part number": "300440157",
        "SOP": "2024-12-01",
        "Delivery country": "India",
        "Delivery city": "Pune",
        "Quotation currency": "INR",
    }
    if layout == "rows":
        for row, (label, value) in enumerate(values.items(), 1):
            sheet.cell(row, 1, label)
            sheet.cell(row, 2, value)
    else:
        for column, (label, value) in enumerate(values.items(), 1):
            sheet.cell(1, column, label)
            sheet.cell(2, column, value)
            sheet.cell(1, column).font = openpyxl.styles.Font(bold=True)
    year_row = 20
    sheet.cell(year_row - 1, 1, "Annual volume")
    for column, year in enumerate(range(2024, 2029), 2):
        sheet.cell(year_row, column, year)
        sheet.cell(year_row + 1, column, 360000)
    hidden = workbook.create_sheet("Finance")
    hidden.sheet_state = "hidden"
    hidden["A1"] = "Quotation currency"
    hidden["B1"] = hidden_currency or "USD"
    workbook.create_named_range("BrokenCurrency", sheet, "#REF!")
    workbook.save(path)
    return path


def _pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4\n%%EOF")
    return path


def test_pdf_plus_xlsm_resolves_all_mandatory_fields(tmp_path):
    workbook = _workbook(tmp_path / "input.xlsm")
    pdf = _pdf(tmp_path / "drawing.pdf")
    result = extract_customer_input_package({}, [workbook, pdf])
    assert result["component_costing_ready"] is True
    assert result["missing_fields"] == []


def test_workbook_commercial_fields_merge_with_pdf_metadata(tmp_path):
    result = extract_customer_input_package({}, [_workbook(tmp_path / "input.xlsx"), _pdf(tmp_path / "drawing.pdf")])
    assert result["fields"]["quotation_currency"]["value"] == "INR"
    assert result["fields"]["drawing_reference"]["value"] == "drawing.pdf"


def test_yearly_series_uses_max_without_summing(tmp_path):
    result = extract_customer_input_package({}, [_workbook(tmp_path / "input.xlsx")])
    annual = result["fields"]["annual_quantity"]
    assert annual["value"] == 360000
    assert annual["qmax"] == 360000
    assert len(annual["quantity_by_year"]) == 5


def test_broken_names_are_ignored_with_warning(tmp_path):
    report = extract_excel_fields(_workbook(tmp_path / "input.xlsx"))
    assert any("broken or unavailable named range" in item for item in report["warnings"])


def test_hidden_financial_sheet_cannot_override_visible_value(tmp_path):
    result = extract_customer_input_package({}, [_workbook(tmp_path / "input.xlsx", hidden_currency="USD")])
    assert result["fields"]["quotation_currency"]["value"] == "INR"


def test_explicit_payload_overrides_workbook_without_blocking(tmp_path):
    payload = {"quotation_currency": "EUR", "_explicit_user_fields": ["quotation_currency"]}
    result = extract_customer_input_package(payload, [_workbook(tmp_path / "input.xlsx")])
    assert result["fields"]["quotation_currency"]["value"] == "EUR"
    assert not any(item["field"] == "quotation_currency" for item in result["conflicts"])


def test_unconfirmed_structured_payload_conflict_blocks(tmp_path):
    result = extract_customer_input_package({"quotation_currency": "EUR"}, [_workbook(tmp_path / "input.xlsx")])
    conflict = next(item for item in result["conflicts"] if item["field"] == "quotation_currency")
    assert conflict["blocking"] is True
    assert result["component_costing_ready"] is False


def test_supplier_currency_does_not_satisfy_quotation_currency():
    result = extract_customer_input_package({"supplier_currency": "CNY"}, [])
    assert "quotation_currency" in result["missing_fields"]


def test_country_normalizes_to_existing_zone():
    result = normalize_delivery_location(country="China", city="Kunshan")
    assert result["normalized_zone"].lower() == "china south pacific"
    assert result["mapping_source"] == "country_to_avocarbon_zone"


def test_repeated_extraction_is_idempotent(tmp_path):
    files = [_workbook(tmp_path / "input.xlsx"), _pdf(tmp_path / "drawing.pdf")]
    first = extract_customer_input_package({}, files)
    second = extract_customer_input_package({}, files)
    assert first["fields"] == second["fields"]
    assert first["conflicts"] == second["conflicts"]


def test_duplicate_filenames_receive_unique_paths(tmp_path):
    first = tmp_path / "drawing.pdf"
    first.write_bytes(b"one")
    second = _unique_upload_path(tmp_path, "drawing.pdf")
    assert second.name == "drawing__2.pdf"


def test_pdf_only_flow_remains_supported(tmp_path):
    pdf = _pdf(tmp_path / "drawing.pdf")
    discovered = discover_customer_input_files([pdf])
    result = extract_customer_input_package({}, discovered)
    assert result["fields"]["drawing_reference"]["value"] == "drawing.pdf"
    assert "annual_quantity" in result["missing_fields"]


def test_different_workbook_layout_is_parsed_by_labels(tmp_path):
    result = extract_customer_input_package({}, [_workbook(tmp_path / "columns.xlsx", layout="columns")])
    assert result["fields"]["customer"]["value"] == "PRABHA ENGINEERING"
    assert result["fields"]["part_number"]["value"] == "300440157"


def test_production_plant_is_not_inferred_from_delivery_location(tmp_path):
    result = extract_customer_input_package({}, [_workbook(tmp_path / "input.xlsx")])
    customer_input = apply_resolution_to_customer_input({}, result)
    assert customer_input.get("production_plant") is None


def test_target_price_is_optional_for_component_costing(tmp_path):
    result = extract_customer_input_package({}, [_workbook(tmp_path / "input.xlsx")])
    assert "target_price" not in result["missing_fields"]
    assert result["component_costing_ready"] is True


def test_candidate_provenance_is_retained():
    result = merge_customer_input_candidates([{
        "field": "customer",
        "value": "A",
        "source_file": "input.csv",
        "source_sheet": "CSV",
        "source_cell": "R1C2",
        "raw_label": "Customer",
        "raw_value": "A",
        "confidence": "high",
        "priority": 3,
        "is_formula": False,
        "formula": None,
        "source_type": "commercial_csv",
    }])
    selected = result["fields"]["customer"]["selected_candidate"]
    assert selected["source_file"] == "input.csv"
    assert selected["source_cell"] == "R1C2"


def test_legacy_currency_mirrors_quotation_currency_only():
    normalized = normalize_customer_input({
        "quotation_currency": "INR",
        "target_price_currency": "EUR",
    })["customer_input"]
    assert normalized["currency"] == "INR"
    assert normalized["target_price_currency"] == "EUR"
