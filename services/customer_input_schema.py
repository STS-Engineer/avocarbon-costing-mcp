def _has_value(value):
    return value not in [None, "", [], {}]


def _first(raw, keys):
    for key in keys:
        value = raw.get(key)
        if _has_value(value):
            return value
    return None


def _number(value):
    if value in [None, ""]:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def normalize_customer_input(raw):
    raw = raw or {}
    normalized = {
        "project_code": _first(raw, ["project_code", "project_number", "systematic_rfq_id"]),
        "customer": _first(raw, ["customer", "customer_name"]),
        "final_customer": _first(raw, ["final_customer", "end_customer"]),
        "product_line": _first(raw, ["product_line", "product_line_name"]),
        "product": _first(raw, ["product", "product_name"]),
        "product_id": _first(raw, ["product_id", "product_reference"]),
        "part_number": _first(raw, ["part_number", "customer_part_number"]),
        "drawing_reference": _first(raw, ["drawing_reference", "drawing_file", "drawing"]),
        "customer_delivery_zone": _first(raw, [
            "customer_delivery_zone",
            "delivery_zone",
            "destination_zone",
            "delivery_area",
        ]),
        "annual_quantity": _number(_first(raw, [
            "annual_quantity",
            "annual_volume",
            "qmax",
            "quantity",
        ])),
        "currency": _first(raw, ["currency", "target_price_currency"]),
        "target_price": _number(_first(raw, ["target_price", "target_price_value"])),
        "sop_date": _first(raw, ["sop_date", "sop", "sop_year"]),
    }

    if not _has_value(normalized["product_id"]) and _has_value(normalized["part_number"]):
        normalized["product_id"] = normalized["part_number"]
    if not _has_value(normalized["part_number"]) and _has_value(normalized["product_id"]):
        normalized["part_number"] = normalized["product_id"]

    missing_inputs = []
    for field_name in [
        "project_code",
        "product_line",
        "product",
        "customer_delivery_zone",
        "annual_quantity",
        "drawing_reference",
    ]:
        if not _has_value(normalized.get(field_name)):
            missing_inputs.append(field_name)
    if not _has_value(normalized.get("product_id")) and not _has_value(normalized.get("part_number")):
        missing_inputs.append("product_id or part_number")

    return {
        "status": "blocked" if missing_inputs else "valid",
        "customer_input": normalized,
        "missing_inputs": missing_inputs,
    }
