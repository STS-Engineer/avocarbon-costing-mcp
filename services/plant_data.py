import re


PLANT_MASTER = {
    "SAME": {
        "plant_country": "Tunisia",
        "operating_currency": "TND",
        "selling_currency": "EUR",
        "dl_rate_operating_per_hour": 11.0,
        "voh_rate_operating_per_hour": 5.0,
        "open_hours_per_year": 5000,
        "fx_operating_to_selling": 3.7,
        "fx_note": "Demo default from Olivier example: 1 EUR = 3.7 TND",
    },
    "Kunshan": {
        "plant_country": "China",
        "operating_currency": "CNY",
        "selling_currency": "CNY",
        "dl_rate_operating_per_hour": 40.0,
        "voh_rate_operating_per_hour": 20.0,
        "open_hours_per_year": 5000,
        "fx_operating_to_selling": 1.0,
        "fx_note": "Demo default same currency",
    },
    "Chennai": {
        "plant_country": "India",
        "operating_currency": "INR",
        "selling_currency": "INR",
        "dl_rate_operating_per_hour": None,
        "voh_rate_operating_per_hour": None,
        "open_hours_per_year": 5000,
        "fx_operating_to_selling": 1.0,
    },
    "Monterrey": {
        "plant_country": "Mexico",
        "operating_currency": "MXN",
        "selling_currency": "USD",
        "dl_rate_operating_per_hour": None,
        "voh_rate_operating_per_hour": None,
        "open_hours_per_year": 5000,
        "fx_operating_to_selling": None,
    },
}


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def get_plant_data(plant_name):
    if not str(plant_name or "").strip():
        return {
            "status": "missing_plant_data",
            "plant_name": plant_name,
            "missing_inputs": ["plant_name"],
            "message": "No plant data found. Do not invent plant rates.",
        }

    lookup = {_normalize(name): name for name in PLANT_MASTER}
    canonical_name = lookup.get(_normalize(plant_name))
    if not canonical_name:
        return {
            "status": "missing_plant_data",
            "plant_name": plant_name,
            "missing_inputs": [f"plant data for {plant_name}"],
            "message": "No plant data found. Do not invent plant rates.",
        }

    data = {
        "status": "found",
        "plant_name": canonical_name,
        **PLANT_MASTER[canonical_name],
    }

    missing_inputs = []
    for field_name in [
        "dl_rate_operating_per_hour",
        "voh_rate_operating_per_hour",
        "open_hours_per_year",
        "fx_operating_to_selling",
    ]:
        if data.get(field_name) in [None, ""]:
            missing_inputs.append(field_name)

    data["missing_inputs"] = missing_inputs
    if missing_inputs:
        data["status"] = "found_with_missing_values"
    return data
