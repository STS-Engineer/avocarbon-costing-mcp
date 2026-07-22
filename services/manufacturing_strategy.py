import csv
import os
import re
import unicodedata
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCT_MATRIX_PATH = BASE_DIR / "data" / "Product matrix.csv"


def normalize_text(value):
    text = str(value or "").strip().lower()
    text = text.replace("/", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _match_tokens(value):
    text = re.sub(r"[^a-z0-9]+", " ", normalize_text(value))
    tokens = []
    for token in text.split():
        if token.endswith("ies") and len(token) > 4:
            token = f"{token[:-3]}y"
        elif token.endswith("s") and len(token) > 3:
            token = token[:-1]
        tokens.append(token)
    return tokens


def _match_key(value):
    return " ".join(_match_tokens(value))


def _ascii_match_key(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return _match_key(text.encode("ascii", "ignore").decode("ascii"))


def get_canonical_products(product_line=None, csv_path=None):
    line_key = _match_key(product_line) if product_line else None
    products = []
    for row in load_product_matrix(csv_path):
        if line_key and _match_key(row["product_line"]) != line_key:
            continue
        product = str(row["product"] or "").strip()
        if product and product not in products:
            products.append(product)
    return products


def resolve_canonical_product(product_line, evidence_values=None, part_number=None, csv_path=None):
    canonical_products = get_canonical_products(product_line, csv_path)
    evidence = [
        str(value).strip()
        for value in (evidence_values or [])
        if value not in [None, "", [], {}]
    ]
    evidence_keys = [_ascii_match_key(value) for value in evidence]
    part_key = re.sub(r"[^a-z0-9]+", "", str(part_number or "").lower())
    candidates = []

    def add_matching_product(expected_key):
        for product in canonical_products:
            if _ascii_match_key(product) == expected_key and product not in candidates:
                candidates.append(product)

    for value_key in evidence_keys:
        for product in canonical_products:
            product_key = _ascii_match_key(product)
            if value_key == product_key and product not in candidates:
                candidates.append(product)

    combined = " ".join(evidence_keys)
    if part_key.startswith("3165001"):
        add_matching_product("fuse choke")
    if any(term in combined for term in ["fuse choke", "choke fuse"]):
        add_matching_product("fuse choke")
    if "self avec fil emaille" in combined and "barre ferrite" in combined:
        add_matching_product("fuse choke")
    if any(term in combined for term in ["rod choke", "choke rod"]):
        add_matching_product("rod choke")
    if any(term in combined for term in ["torroid choke", "toroid choke", "toroidal choke"]):
        add_matching_product("torroid choke")

    if len(candidates) == 1:
        return {
            "status": "resolved",
            "canonical_product": candidates[0],
            "candidates": candidates,
            "evidence": evidence,
            "part_number": part_number,
        }
    if len(candidates) > 1:
        return {
            "status": "ambiguous",
            "canonical_product": None,
            "candidates": candidates,
            "evidence": evidence,
            "part_number": part_number,
            "message": "Multiple Product Matrix products match; explicit selection is required.",
        }
    return {
        "status": "not_resolved",
        "canonical_product": None,
        "candidates": canonical_products,
        "evidence": evidence,
        "part_number": part_number,
        "message": "No unique Product Matrix product could be resolved.",
    }


def normalize_delivery_zone(value):
    zone_key = _match_key(value)
    aliases = {
        "europe": "Europe",
        "india": "India",
        "north america": "North America",
        "south america": "South America",
        "china south pacific": "China south Pacific",
        "china": "China south Pacific",
        "pr china": "China south Pacific",
        "tunisia": "Africa",
        "france": "Europe",
        "japan korea": "Japan Korea",
    }
    return aliases.get(zone_key, str(value or "").strip())


def _parse_percent(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("%", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _resolve_product_matrix_path(csv_path=None):
    configured = csv_path or os.getenv("PRODUCT_MATRIX_CSV_PATH")
    if configured:
        path = Path(configured)
        candidates = [path]
        if not path.is_absolute():
            candidates.append(BASE_DIR / path)
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return DEFAULT_PRODUCT_MATRIX_PATH


def load_product_matrix(csv_path=None):
    path = _resolve_product_matrix_path(csv_path)
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.reader(handle))

    if not raw_rows:
        return []

    headers = [str(header or "").strip() for header in raw_rows[0]]
    header_keys = [_match_key(header) for header in headers]

    product_line_index = 0
    product_index = next(
        (index for index, key in enumerate(header_keys) if key == "product"),
        1,
    )
    van_index = next(
        (index for index, key in enumerate(header_keys) if key == "van"),
        None,
    )

    zone_indexes = [
        index
        for index, header in enumerate(headers)
        if index not in [product_line_index, product_index, van_index] and header
    ]

    records = []
    for raw_row in raw_rows[1:]:
        if not any(str(cell or "").strip() for cell in raw_row):
            continue

        product_line = _cell(raw_row, product_line_index)
        product = _cell(raw_row, product_index)
        if not product_line or not product:
            continue
        if _match_key(product_line).startswith("checked"):
            continue

        zones = {
            headers[index].strip(): _cell(raw_row, index)
            for index in zone_indexes
        }
        records.append({
            "product_line": product_line,
            "product": product,
            "zones": zones,
            "target_van_percent": _parse_percent(_cell(raw_row, van_index)),
            "source_path": str(path),
        })

    return records


def select_manufacturing_strategy(product_line, product, customer_delivery_zone):
    missing_inputs = []
    if not str(product_line or "").strip():
        missing_inputs.append("product_line")
    if not str(product or "").strip():
        missing_inputs.append("product")
    if not str(customer_delivery_zone or "").strip():
        missing_inputs.append("customer_delivery_zone")

    if missing_inputs:
        return {
            "status": "missing_strategy",
            "missing_inputs": missing_inputs,
            "message": "No manufacturing strategy found. Do not invent plant.",
        }

    rows = load_product_matrix()
    if not rows:
        return {
            "status": "missing_strategy",
            "missing_inputs": ["Product Matrix CSV"],
            "message": "No manufacturing strategy found. Do not invent plant.",
        }

    product_line_key = _match_key(product_line)
    product_key = _match_key(product)
    delivery_zone_key = _match_key(normalize_delivery_zone(customer_delivery_zone))

    for row in rows:
        if _match_key(row["product_line"]) != product_line_key:
            continue
        if _match_key(row["product"]) != product_key:
            continue

        for zone_name, production_plant in row["zones"].items():
            if _match_key(zone_name) != delivery_zone_key:
                continue
            if not production_plant:
                break
            return {
                "status": "found",
                "product_line": row["product_line"],
                "product": row["product"],
                "customer_delivery_zone": zone_name,
                "delivery_zone": zone_name,
                "production_plant": production_plant,
                "target_van_percent": row["target_van_percent"],
                "source": "Product Matrix CSV",
            }

    return {
        "status": "missing_strategy",
        "product_line": product_line,
        "product": product,
        "customer_delivery_zone": customer_delivery_zone,
        "delivery_zone": customer_delivery_zone,
        "missing_inputs": ["manufacturing strategy for product and delivery zone"],
        "message": "No manufacturing strategy found. Do not invent plant.",
    }
