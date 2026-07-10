import csv
import os
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_UNIT_TABLE_PATH = BASE_DIR / "data" / "AVOCarbon_Unit_Table_Update_Costing.csv"

DEMO_FALLBACK = {
    "SAME": {
        "plant": "SAME",
        "plant_alias": "ElFahs",
        "zone": "Europe",
        "selling_currency": "EUR",
        "operating_currency": "TND",
        "dl_rate_operating_per_hour": 8.5,
        "voh_rate_operating_per_hour": 5.1,
        "foh_percent_dc": 110,
        "fee_percent_dc": 70,
        "company_tax_rate": 30,
        "number_of_shifts": None,
        "open_hours_per_year": 4224,
    },
    "ELFAHS": {
        "plant": "ElFahs",
        "plant_alias": "SAME",
        "zone": "Europe",
        "selling_currency": "EUR",
        "operating_currency": "TND",
        "dl_rate_operating_per_hour": 8.5,
        "voh_rate_operating_per_hour": 5.1,
        "foh_percent_dc": 110,
        "fee_percent_dc": 70,
        "company_tax_rate": 30,
        "number_of_shifts": None,
        "open_hours_per_year": 4224,
    },
    "KUNSHAN": {
        "plant": "Kunshan",
        "plant_alias": None,
        "zone": "Asia",
        "selling_currency": "CNY",
        "operating_currency": "CNY",
        "dl_rate_operating_per_hour": 32,
        "voh_rate_operating_per_hour": 9.6,
        "foh_percent_dc": 77,
        "fee_percent_dc": 56,
        "company_tax_rate": 15,
        "number_of_shifts": None,
        "open_hours_per_year": 5808,
    },
}


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _header_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _number(value):
    if value in [None, ""]:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _find_unit_table_path(csv_path=None):
    explicit = csv_path or os.getenv("UNIT_TABLE_CSV_PATH")
    if explicit:
        path = Path(explicit)
        return path if path.exists() else path
    if DEFAULT_UNIT_TABLE_PATH.exists():
        return DEFAULT_UNIT_TABLE_PATH
    matches = list((BASE_DIR / "data").glob("AVOCarbon_Unit_Table_Update_Costing*.csv"))
    return matches[0] if matches else DEFAULT_UNIT_TABLE_PATH


def _load_rows(csv_path=None):
    path = _find_unit_table_path(csv_path)
    if not path.exists():
        return [], str(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle)), str(path)


def _value(row, candidates):
    keyed = {_header_key(key): value for key, value in row.items()}
    for candidate in candidates:
        value = keyed.get(_header_key(candidate))
        if value not in [None, ""]:
            return str(value).strip()
    return None


def _apply_demo_fallback(data):
    key = _normalize(data.get("plant"))
    fallback_key = None
    if key in ["same", "elfahs"]:
        fallback_key = "SAME"
    elif key == "kunshan":
        fallback_key = "KUNSHAN"
    if not fallback_key:
        return data

    fallback = DEMO_FALLBACK[fallback_key]
    assumptions = list(data.get("assumptions") or [])
    for field_name, fallback_value in fallback.items():
        if data.get(field_name) in [None, ""] and fallback_value not in [None, ""]:
            data[field_name] = fallback_value
            assumptions.append(f"{field_name} filled from demo fallback")
    data["assumptions"] = list(dict.fromkeys(assumptions))
    if assumptions:
        data["demo_fallback_used"] = True
    return data


def get_unit_data(production_plant):
    if not str(production_plant or "").strip():
        return {
            "status": "missing_unit_data",
            "plant": production_plant,
            "missing_inputs": ["production_plant"],
        }

    rows, source_path = _load_rows()
    target_keys = {_normalize(production_plant)}
    if _normalize(production_plant) == "same":
        target_keys.add("elfahs")
    if _normalize(production_plant) == "elfahs":
        target_keys.add("same")

    matched_row = None
    for row in rows:
        unit = _value(row, ["Plant", "Unit"])
        if _normalize(unit) in target_keys:
            matched_row = row
            break

    if matched_row:
        plant = _value(matched_row, ["Plant", "Unit"]) or production_plant
        data = {
            "status": "found",
            "plant": plant,
            "plant_alias": "ElFahs" if _normalize(production_plant) == "same" and _normalize(plant) != "same" else None,
            "zone": _value(matched_row, ["Zone"]),
            "selling_currency": _value(matched_row, ["Selling currency"]),
            "operating_currency": _value(matched_row, ["Operating currency"]),
            "dl_rate_operating_per_hour": _number(_value(matched_row, [
                "DL Rate in operating currency",
                "Direct labor cost / hour (operating currency)",
            ])),
            "voh_rate_operating_per_hour": _number(_value(matched_row, [
                "VOH base Rate in operating currency",
                "Base variable overhead cost / hour (operating currency)",
            ])),
            "foh_percent_dc": _number(_value(matched_row, ["FOH%/DC", "% FOH/DC"])),
            "fee_percent_dc": _number(_value(matched_row, ["FEE%/DC", "% FEE/DC"])),
            "company_tax_rate": _number(_value(matched_row, ["Company Tax Rate", "% Company tax"])),
            "number_of_shifts": _number(_value(matched_row, ["Number of shifts"])),
            "open_hours_per_year": _number(_value(matched_row, ["Open hour / year", "Open hours/year"])),
            "source": source_path,
            "assumptions": [],
        }
    else:
        fallback = DEMO_FALLBACK.get(_normalize(production_plant).upper())
        if not fallback:
            return {
                "status": "missing_unit_data",
                "plant": production_plant,
                "source": source_path,
                "missing_inputs": [f"unit data for {production_plant}"],
            }
        data = {
            "status": "found",
            **fallback,
            "source": "demo fallback",
            "assumptions": ["Unit table CSV row missing; demo fallback used"],
            "demo_fallback_used": True,
        }

    data = _apply_demo_fallback(data)
    required_fields = [
        "selling_currency",
        "operating_currency",
        "dl_rate_operating_per_hour",
        "voh_rate_operating_per_hour",
        "foh_percent_dc",
        "fee_percent_dc",
        "company_tax_rate",
        "open_hours_per_year",
    ]
    data["missing_inputs"] = [
        field_name for field_name in required_fields if data.get(field_name) in [None, ""]
    ]
    if data["missing_inputs"]:
        data["status"] = "found_with_missing_values"
    return data
