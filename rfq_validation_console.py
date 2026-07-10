import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv
from psycopg2.extras import Json, RealDictCursor

from services.choke_orchestrator import run_choke_orchestration
from services.choke_workspace_orchestrator import build_choke_workspace_orchestration
from services.external_component_agent import run_external_component_agent

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
RFQ_DATABASE_URL = os.getenv("RFQ_DATABASE_URL")

st.set_page_config(page_title="AVOCarbon RFQ Validation", layout="wide")

st.title("AVOCarbon RFQ Validation Console")
st.caption("Step 0: validate customer input before costing")


def db_connect():
    if not DATABASE_URL:
        st.error("DATABASE_URL is not configured.")
        st.stop()

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode=os.getenv("PGSSLMODE", "require"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )


def rfq_db_connect():
    if not RFQ_DATABASE_URL:
        return None

    return psycopg2.connect(
        RFQ_DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode=os.getenv("RFQ_PGSSLMODE", os.getenv("PGSSLMODE", "require")),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )


def ensure_table():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_json_records (
                    agent_json_record_id SERIAL PRIMARY KEY,
                    project_code TEXT NOT NULL,
                    product_reference TEXT NULL,
                    json_type TEXT NOT NULL,
                    source_agent TEXT NULL,
                    validation_status TEXT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS rfq_validation_summary (
                    rfq_validation_summary_id SERIAL PRIMARY KEY,
                    agent_json_record_id INTEGER REFERENCES agent_json_records(agent_json_record_id),
                    project_code TEXT NOT NULL,
                    customer TEXT NULL,
                    product_reference TEXT NULL,
                    package_status TEXT NULL,
                    can_continue_to_costing BOOLEAN DEFAULT FALSE,
                    missing_count INTEGER DEFAULT 0,
                    blocking_missing_count INTEGER DEFAULT 0,
                    next_agent TEXT NULL,
                    blocking_reason TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS rfq_validation_issues (
                    rfq_validation_issue_id SERIAL PRIMARY KEY,
                    agent_json_record_id INTEGER REFERENCES agent_json_records(agent_json_record_id),
                    project_code TEXT NOT NULL,
                    product_reference TEXT NULL,
                    issue_level TEXT NULL,
                    issue_field TEXT NULL,
                    issue_description TEXT NULL,
                    is_blocking BOOLEAN DEFAULT FALSE,
                    issue_status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS rfq_validation_products (
                    rfq_validation_product_id SERIAL PRIMARY KEY,
                    agent_json_record_id INTEGER REFERENCES agent_json_records(agent_json_record_id),
                    project_code TEXT NOT NULL,
                    product_reference TEXT NULL,
                    product_line TEXT NULL,
                    product_family TEXT NULL,
                    drawing_status TEXT NULL,
                    drawing_reference TEXT NULL,
                    max_quantity_status TEXT NULL,
                    max_quantity_value TEXT NULL,
                    delivery_zone TEXT NULL,
                    target_price_status TEXT NULL,
                    target_price_value TEXT NULL,
                    sop_status TEXT NULL,
                    sop_value TEXT NULL,
                    validation_status TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS rfq_validation_components (
                    rfq_validation_component_id SERIAL PRIMARY KEY,
                    agent_json_record_id INTEGER REFERENCES agent_json_records(agent_json_record_id),
                    project_code TEXT NOT NULL,
                    product_reference TEXT NULL,
                    component_name TEXT NULL,
                    component_code_or_drawing_number TEXT NULL,
                    component_family TEXT NULL,
                    quantity_status TEXT NULL,
                    quantity_value TEXT NULL,
                    drawing_status TEXT NULL,
                    plant_to_deliver TEXT NULL,
                    internal_or_external_status TEXT NULL,
                    internal_or_external_value TEXT NULL,
                    validation_status TEXT NULL,
                    is_blocking BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS component_costing_queue (
                    component_costing_queue_id SERIAL PRIMARY KEY,
                    source_validation_component_id INTEGER NULL,
                    agent_json_record_id INTEGER NULL REFERENCES agent_json_records(agent_json_record_id),
                    project_code TEXT NOT NULL,
                    product_reference TEXT NULL,
                    component_name TEXT NULL,
                    component_code_or_drawing_number TEXT NULL,
                    component_family TEXT NULL,
                    costing_route TEXT NULL,
                    costing_status TEXT DEFAULT 'pending',
                    required_agent TEXT NULL,
                    blocking_reason TEXT NULL,
                    country_to_produce TEXT NULL,
                    max_quantity_per_year TEXT NULL,
                    component_json JSONB NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_test_scenarios (
                    scenario_id SERIAL PRIMARY KEY,
                    scenario_name TEXT NOT NULL,
                    scenario_type TEXT NOT NULL,
                    agent_under_test TEXT NOT NULL,
                    input_required TEXT NOT NULL,
                    expected_output TEXT NOT NULL,
                    actual_output TEXT NULL,
                    test_status TEXT DEFAULT 'not_run',
                    notes TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS component_costing_tests (
                    component_costing_test_id SERIAL PRIMARY KEY,
                    scenario_name TEXT NOT NULL,
                    project_code TEXT NOT NULL,
                    customer TEXT NULL,
                    product_reference TEXT NULL,
                    component_reference TEXT NULL,
                    component_name TEXT NULL,
                    product_family TEXT NULL,
                    costing_agent TEXT NOT NULL,
                    input_payload JSONB NOT NULL,
                    expected_output TEXT NULL,
                    actual_output JSONB NULL,
                    test_status TEXT DEFAULT 'not_run',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS costing_workflow_routes (
                    costing_workflow_route_id SERIAL PRIMARY KEY,
                    project_code TEXT NOT NULL,
                    product_reference TEXT NULL,
                    product_name TEXT NULL,
                    product_family TEXT NOT NULL,
                    workflow_type TEXT NOT NULL,
                    workflow_status TEXT DEFAULT 'draft',
                    route_reason TEXT NULL,
                    required_steps JSONB NOT NULL,
                    missing_inputs JSONB NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()

def extract_summary(payload):
    next_action = payload.get("next_action", {})
    customer_input_validation = payload.get("customer_input_validation", {})
    missing = payload.get("missing_information_summary", [])

    return {
        "package_status": customer_input_validation.get("package_status"),
        "can_continue_to_costing": next_action.get("can_continue_to_costing"),
        "next_agent": next_action.get("agent_to_call_next"),
        "missing_count": len(missing) if isinstance(missing, list) else 0,
        "reason": customer_input_validation.get("reason") or next_action.get("reason"),
    }


def load_rfq_data_from_db(systematic_rfq_id):
    if not RFQ_DATABASE_URL:
        return None, "RFQ_DATABASE_URL is not configured. Paste rfq_data JSON manually."

    if not systematic_rfq_id:
        return None, "systematic_rfq_id is required"

    try:
        with rfq_db_connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("""
                        SELECT rfq_data
                        FROM rfq
                        WHERE systematic_rfq_id = %s
                        LIMIT 1
                    """, (systematic_rfq_id,))
                    row = cur.fetchone()
                except Exception:
                    conn.rollback()
                    cur.execute("""
                        SELECT rfq_data
                        FROM rfq
                        WHERE rfq_data->>'systematic_rfq_id' = %s
                            OR rfq_data->>'systematicRfqId' = %s
                            OR rfq_data->>'project_number' = %s
                            OR rfq_data->>'project_code' = %s
                        LIMIT 1
                    """, (
                        systematic_rfq_id,
                        systematic_rfq_id,
                        systematic_rfq_id,
                        systematic_rfq_id,
                    ))
                    row = cur.fetchone()

        if not row:
            return None, f"RFQ not found for systematic_rfq_id: {systematic_rfq_id}"

        rfq_data = row["rfq_data"]
        if isinstance(rfq_data, str):
            rfq_data = json.loads(rfq_data)

        return rfq_data, None
    except Exception as exc:
        return None, f"Failed to load RFQ from DB: {exc}"


def get_first_value(data, paths):
    return get_nested_value(data, paths)


def normalize_rfq_list(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def extract_rfq_products(rfq_data):
    products = normalize_rfq_list(get_dict_value(rfq_data, "products"))
    if products:
        return products

    single_product = {
        "part_number": get_first_value(rfq_data, [
            ["part_number"],
            ["customer_part_number"],
            ["customer_pn"],
            ["product_part_number"],
        ]),
        "product_name": get_first_value(rfq_data, [
            ["product_name"],
            ["product"],
        ]),
        "product_line": get_first_value(rfq_data, [
            ["product_line"],
            ["product_type"],
        ]),
        "quantity": get_first_value(rfq_data, [
            ["quantity"],
            ["annual_quantity"],
            ["annual_volume"],
            ["qmax"],
        ]),
        "target_price": get_first_value(rfq_data, [
            ["target_price"],
            ["target_price_eur"],
        ]),
        "currency": get_first_value(rfq_data, [
            ["currency"],
            ["target_price_currency"],
        ]),
        "sop": get_first_value(rfq_data, [
            ["sop"],
            ["sop_date"],
        ]),
        "sop_year": get_first_value(rfq_data, [
            ["sop_year"],
        ]),
        "delivery_zone": get_first_value(rfq_data, [
            ["delivery_zone"],
            ["destination_zone"],
        ]),
    }

    if any(value not in [None, ""] for value in single_product.values()):
        return [single_product]

    return []


def extract_rfq_files(rfq_data):
    documents = []

    for source_key in ["rfq_files", "costing_files"]:
        for file_row in normalize_rfq_list(get_dict_value(rfq_data, source_key)):
            documents.append({
                "source": source_key,
                "file_name": (
                    file_row.get("file_name")
                    or file_row.get("filename")
                    or file_row.get("name")
                ),
                "content_type": (
                    file_row.get("content_type")
                    or file_row.get("file_type")
                    or file_row.get("mime_type")
                ),
                "url": (
                    file_row.get("download_url")
                    or file_row.get("url")
                    or file_row.get("file_url")
                    or file_row.get("blob_url")
                    or file_row.get("path")
                ),
                "download_url": (
                    file_row.get("download_url")
                    or file_row.get("url")
                    or file_row.get("file_url")
                    or file_row.get("blob_url")
                    or file_row.get("path")
                ),
                "uploaded_by": file_row.get("uploaded_by"),
                "uploaded_at": file_row.get("uploaded_at"),
                "status": "available",
            })

    sharepoint = get_dict_value(rfq_data, "sharepoint")
    for sharepoint_item in normalize_rfq_list(sharepoint):
        documents.append({
            "source": "sharepoint",
            "file_name": (
                get_nested_value(sharepoint_item, [["file_name"], ["name"], ["site_name"]])
                or "SharePoint source"
            ),
            "content_type": get_nested_value(sharepoint_item, [["content_type"], ["type"]]),
            "url": get_nested_value(sharepoint_item, [
                ["download_url"],
                ["url"],
                ["web_url"],
                ["site_url"],
            ]),
            "download_url": get_nested_value(sharepoint_item, [
                ["download_url"],
                ["url"],
                ["web_url"],
                ["site_url"],
            ]),
            "uploaded_by": get_nested_value(sharepoint_item, [["uploaded_by"], ["owner"]]),
            "uploaded_at": get_nested_value(sharepoint_item, [["uploaded_at"], ["created_at"]]),
            "status": "available",
            "raw_sharepoint": sharepoint_item,
        })

    return documents


def append_missing(missing, level, field, description, blocking=True):
    missing.append({
        "level": level,
        "field": field,
        "missing_information": field,
        "detail": description,
        "reason": description,
        "blocking": blocking,
    })


def field_has_value(value):
    return value not in [None, "", [], {}]


PRODUCT_LINE_ACRONYMS = {
    "ASS": "Assembly",
    "BRU": "Brushes",
    "CHO": "Chokes",
    "ADM": "Advanced Material",
    "FRI": "Friction",
    "SEA": "Seals",
}

PRODUCT_TYPE_FAMILIES = {
    "Chokes": "Choke",
    "Assembly": "Assembly",
    "Brushes": "Brush",
    "Seals": "Seal",
    "Advanced Material": "Advanced Material",
    "Friction": "Friction",
}


def infer_product_line_from_rfq_id(rfq_id):
    if not rfq_id:
        return None

    parts = re.split(r"[-_/\\\s]+", str(rfq_id).upper())
    for part in parts:
        if part in PRODUCT_LINE_ACRONYMS:
            return PRODUCT_LINE_ACRONYMS[part]

    return None


def normalize_product_line(value):
    if not value:
        return None

    text = str(value).strip()
    normalized = normalize_lookup_key(text)

    for acronym, product_line in PRODUCT_LINE_ACRONYMS.items():
        if normalized == normalize_lookup_key(acronym):
            return product_line

    for product_line in PRODUCT_TYPE_FAMILIES:
        if normalized == normalize_lookup_key(product_line):
            return product_line

    aliases = {
        "choke": "Chokes",
        "chokes": "Chokes",
        "assembly": "Assembly",
        "assemblies": "Assembly",
        "brush": "Brushes",
        "brushes": "Brushes",
        "seal": "Seals",
        "seals": "Seals",
        "advancedmaterial": "Advanced Material",
        "friction": "Friction",
    }

    return aliases.get(normalized, text)


def product_type_family_from_product_line(product_line):
    normalized_product_line = normalize_product_line(product_line)
    return PRODUCT_TYPE_FAMILIES.get(normalized_product_line)


def extract_sharepoint_text(value):
    if not value:
        return ""

    if isinstance(value, list):
        return " ".join(extract_sharepoint_text(item) for item in value)

    if isinstance(value, dict):
        fields = [
            "folder_path",
            "path",
            "url",
            "web_url",
            "site_url",
            "name",
            "site_name",
            "library",
        ]
        return " ".join(
            str(get_nested_value(value, [[field]]) or "")
            for field in fields
        )

    return str(value)


def infer_product_line_from_sharepoint(rfq_data):
    sharepoint_text = extract_sharepoint_text(get_dict_value(rfq_data, "sharepoint")).lower()

    if not sharepoint_text:
        return None

    folder_markers = [
        ("choke", "Chokes"),
        ("assembly", "Assembly"),
        ("assembl", "Assembly"),
        ("brush", "Brushes"),
        ("seal", "Seals"),
        ("advanced material", "Advanced Material"),
        ("advanced_material", "Advanced Material"),
        ("friction", "Friction"),
    ]

    for marker, product_line in folder_markers:
        if marker in sharepoint_text:
            return product_line

    return None


def resolve_product_line(product, rfq_data, systematic_rfq_id):
    explicit_product_line = get_first_value(product, [
        ["product_line"],
        ["product_type"],
        ["line"],
    ])

    if field_has_value(explicit_product_line):
        return normalize_product_line(explicit_product_line), "provided"

    product_line_acronym = (
        get_first_value(product, [["product_line_acronym"], ["line_acronym"]])
        or get_first_value(rfq_data, [["product_line_acronym"], ["line_acronym"]])
    )

    product_line = normalize_product_line(product_line_acronym)
    if field_has_value(product_line):
        return product_line, "inferred_from_product_line_acronym"

    product_line = infer_product_line_from_rfq_id(systematic_rfq_id)
    if field_has_value(product_line):
        return product_line, "inferred_from_systematic_rfq_id"

    product_line = infer_product_line_from_sharepoint(rfq_data)
    if field_has_value(product_line):
        return product_line, "inferred_from_sharepoint_folder_path"

    return None, None


def resolve_product_sop(product, rfq_data):
    product_sop = get_first_value(product, [
        ["sop"],
        ["sop_date"],
        ["sop_year"],
    ])
    product_sop_year = get_first_value(product, [["sop_year"]])

    if field_has_value(product_sop):
        return product_sop, product_sop_year, "product_level"

    project_sop = get_first_value(rfq_data, [
        ["sop"],
        ["sop_date"],
        ["sop_year"],
    ])
    project_sop_year = get_first_value(rfq_data, [["sop_year"]])

    if field_has_value(project_sop):
        return project_sop, project_sop_year or project_sop, "project_level"

    return None, None, None


def resolve_product_name(product, rfq_data):
    product_name = get_first_value(product, [
        ["product_name"],
        ["name"],
        ["product"],
    ])

    if field_has_value(product_name):
        return product_name, "product_level"

    project_product_name = get_first_value(rfq_data, [
        ["product_name"],
        ["product"],
    ])

    if field_has_value(project_product_name):
        return project_product_name, "project_level"

    return None, None


def build_validation_from_rfq_data(rfq_data):
    systematic_rfq_id = get_first_value(rfq_data, [
        ["systematic_rfq_id"],
        ["systematicRfqId"],
        ["project_number"],
        ["project_code"],
        ["rfq_id"],
    ])
    project_code = normalize_project_code(systematic_rfq_id)
    customer_name = get_first_value(rfq_data, [
        ["customer_name"],
        ["customer"],
        ["account_name"],
    ])
    delivery_zone = get_first_value(rfq_data, [
        ["delivery_zone"],
        ["destination_zone"],
        ["zone"],
    ])
    contact_email = get_first_value(rfq_data, [
        ["contact_email"],
        ["customer_contact_email"],
        ["contact", "email"],
        ["customer_contact", "email"],
    ])
    products = extract_rfq_products(rfq_data)
    documents = extract_rfq_files(rfq_data)
    missing = []

    if not field_has_value(project_code):
        append_missing(missing, "project", "project_code", "Project code / systematic RFQ ID is missing")
    if not field_has_value(customer_name):
        append_missing(missing, "project", "customer", "Customer name is missing")
    if not field_has_value(contact_email):
        append_missing(missing, "project", "contact_email", "Customer contact email is missing")
    if not products:
        append_missing(missing, "project", "product_list", "Product list is missing")
    if not field_has_value(delivery_zone):
        append_missing(missing, "project", "delivery_zone", "Delivery zone is missing")
    if not documents:
        append_missing(missing, "documents", "drawing_or_rfq_file", "Drawing or RFQ file is missing")

    products_validation = []
    for index, product in enumerate(products, start=1):
        part_number = get_first_value(product, [
            ["part_number"],
            ["customer_part_number"],
            ["customer_reference"],
            ["product_reference"],
        ])
        quantity = get_first_value(product, [
            ["quantity"],
            ["annual_volume"],
            ["annual_quantity"],
            ["qmax"],
        ])
        product_delivery_zone = get_first_value(product, [
            ["delivery_zone"],
            ["destination_zone"],
        ]) or delivery_zone
        sop, sop_year, sop_source = resolve_product_sop(product, rfq_data)
        product_name, product_name_source = resolve_product_name(product, rfq_data)
        product_line, product_line_source = resolve_product_line(
            product=product,
            rfq_data=rfq_data,
            systematic_rfq_id=systematic_rfq_id,
        )
        product_type_family = product_type_family_from_product_line(product_line)

        product_missing = []
        if not field_has_value(quantity):
            product_missing.append("annual_quantity")
            append_missing(
                missing,
                "product",
                f"products[{index}].annual_quantity",
                f"Annual quantity is missing for product {part_number or index}",
            )
        if not field_has_value(product_delivery_zone):
            product_missing.append("delivery_zone")
            append_missing(
                missing,
                "product",
                f"products[{index}].delivery_zone",
                f"Delivery zone is missing for product {part_number or index}",
            )
        if not field_has_value(sop) and not field_has_value(sop_year):
            product_missing.append("sop")
            append_missing(
                missing,
                "product",
                f"products[{index}].sop",
                f"SOP or SOP year is missing for product {part_number or index}",
            )
        if not field_has_value(product_line):
            product_missing.append("product_line")
            append_missing(
                missing,
                "product",
                f"products[{index}].product_line",
                f"Product line is missing for product {part_number or index}",
            )

        products_validation.append({
            "part_number": part_number,
            "product_name": product_name,
            "product_name_source": product_name_source,
            "product_line": product_line,
            "product_line_source": product_line_source,
            "product_type_family": product_type_family,
            "quantity": quantity,
            "annual_volume": get_first_value(product, [
                ["annual_volume"],
                ["annual_quantity"],
            ]) or quantity,
            "target_price": get_first_value(product, [
                ["target_price"],
                ["target_price_eur"],
            ]),
            "currency": get_first_value(product, [
                ["currency"],
                ["target_price_currency"],
            ]),
            "sop": sop,
            "sop_year": sop_year,
            "sop_source": sop_source,
            "delivery_zone": product_delivery_zone,
            "validation_status": "incomplete" if product_missing else "structured_data_available",
            "missing_fields": product_missing,
        })

    mandatory_missing = any(item.get("blocking") for item in missing)
    package_status = (
        "incomplete"
        if mandatory_missing
        else "ready_for_agent_document_validation"
    )

    return {
        "customer_input_validation": {
            "package_status": package_status,
            "reason": (
                "Mandatory structured RFQ information is missing."
                if mandatory_missing
                else (
                    "Structured RFQ data is complete. Attached documents still "
                    "require triage agent validation before costing."
                )
            ),
        },
        "project_validation": {
            "project_name": get_first_value(rfq_data, [
                ["project_name"],
                ["name"],
            ]),
            "project_number_or_rfq_number": project_code,
            "customer": customer_name,
            "delivery_zone": delivery_zone,
            "contact_name": get_first_value(rfq_data, [
                ["contact_name"],
                ["customer_contact_name"],
                ["contact", "name"],
                ["customer_contact", "name"],
            ]),
            "contact_email": contact_email,
            "contact_role": get_first_value(rfq_data, [
                ["contact_role"],
                ["customer_contact_role"],
                ["contact", "role"],
                ["customer_contact", "role"],
            ]),
            "quotation_expected_date": get_first_value(rfq_data, [
                ["quotation_expected_date"],
                ["quote_due_date"],
                ["expected_quote_date"],
            ]),
            "rfq_reception_date": get_first_value(rfq_data, [
                ["rfq_reception_date"],
                ["received_at"],
                ["created_at"],
            ]),
            "expected_payment_terms": get_first_value(rfq_data, [
                ["expected_payment_terms"],
                ["payment_terms"],
            ]),
            "expected_delivery_conditions": get_first_value(rfq_data, [
                ["expected_delivery_conditions"],
                ["delivery_conditions"],
                ["incoterm"],
            ]),
        },
        "products_validation": products_validation,
        "components_validation": [],
        "documents_validation": documents,
        "missing_information_summary": missing,
        "next_action": {
            "can_continue_to_costing": False,
            "agent_to_call_next": (
                "KAM / sales completion"
                if mandatory_missing
                else "RFQ document validation agent"
            ),
            "reason": (
                "Missing mandatory structured RFQ information."
                if mandatory_missing
                else (
                    "Structured RFQ data is complete. Attached documents still "
                    "require triage agent validation before costing."
                )
            ),
        },
    }

def normalize_lookup_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def get_dict_value(data, key):
    if not isinstance(data, dict):
        return None

    if key in data:
        return data[key]

    wanted = normalize_lookup_key(key)
    for existing_key, value in data.items():
        if normalize_lookup_key(existing_key) == wanted:
            return value

    return None


def get_nested_entry(data, paths):
    for path in paths:
        current = data
        ok = True
        for key in path:
            if isinstance(current, dict):
                current = get_dict_value(current, key)
                if current is None:
                    ok = False
                    break
            elif isinstance(current, list) and isinstance(key, int) and len(current) > key:
                current = current[key]
            else:
                ok = False
                break
        if ok:
            return current
    return None


def unwrap_validation_value(value):
    if isinstance(value, dict):
        for key in ["value", "extracted_value", "normalized_value", "answer", "text"]:
            nested = get_dict_value(value, key)
            if nested not in [None, ""]:
                return unwrap_validation_value(nested)

        if len(value) == 1:
            return unwrap_validation_value(next(iter(value.values())))

        return None

    return value


def get_nested_value(data, paths):
    return unwrap_validation_value(get_nested_entry(data, paths))


def get_nested_status(data, paths):
    for path in paths:
        entry = get_nested_entry(data, [path])

        if isinstance(entry, dict):
            for key in ["status", "validation_status", "state"]:
                value = get_dict_value(entry, key)
                if value not in [None, ""]:
                    return unwrap_validation_value(value)

        terminal_key = str(path[-1]).lower() if path else ""
        if "status" in terminal_key or "state" in terminal_key:
            value = unwrap_validation_value(entry)
            if value not in [None, ""]:
                return value

    return None


def extract_project_code(payload):
    return get_nested_value(payload, [
        ["project_validation", "project_number_or_rfq_number"],
        ["project_validation", "project_number"],
        ["project_validation", "required_information", "project_number"],
        ["project", "project_code"],
        ["project", "project_id"],
    ]) or "UNKNOWN_PROJECT"

def normalize_project_code(value):
    if not value:
        return None

    text = str(value).strip()
    match = re.search(r"(?:Quote\s*ID\s*)?([0-9]+(?:-[A-Za-z0-9]+)+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return re.sub(r"^\s*Quote\s*ID\s*", "", text, flags=re.IGNORECASE).strip()


def as_record_list(value, list_keys):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    if isinstance(value, dict):
        for key in list_keys:
            nested = get_dict_value(value, key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]

        return [value]

    return []


PRODUCT_REFERENCE_PATHS = [
    ["product_reference"],
    ["part_number"],
    ["customer_part_number"],
    ["customer_reference"],
    ["reference"],
    ["product", "reference"],
    ["product", "product_reference"],
    ["product", "part_number"],
    ["product_identification", "product_reference"],
]

COMPONENT_PRODUCT_REFERENCE_PATHS = [
    ["parent_product_part_number"],
    ["parent_part_number"],
    ["parent_product_reference"],
    ["parent_product", "part_number"],
    ["parent_product", "product_reference"],
    ["parent_product", "reference"],
    ["product_reference"],
    ["customer_product_reference"],
    ["customer_reference"],
]

PRODUCT_LIST_KEYS = [
    "products",
    "product_list",
    "items",
    "validated_products",
    "products_validation",
]

COMPONENT_LIST_KEYS = [
    "components",
    "component_list",
    "items",
    "validated_components",
    "components_validation",
]


def get_component_parent_product_reference(component):
    return get_nested_value(component, COMPONENT_PRODUCT_REFERENCE_PATHS)


def get_child_component_records(component):
    if not isinstance(component, dict):
        return []

    for key in ["components", "components_validation", "component_list", "subcomponents", "items"]:
        nested = get_dict_value(component, key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]

    return []


def get_component_validation_records(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    if isinstance(value, dict):
        nested_components = get_child_component_records(value)
        if nested_components and get_component_parent_product_reference(value):
            return [value]
        if nested_components:
            return nested_components
        return [value]

    return []


def infer_component_family(*values):
    text = " ".join(str(value or "") for value in values).lower()

    if "spring" in text:
        return "Spring"
    if any(keyword in text for keyword in ["choke", "inductor", "ferrite"]):
        return "Choke/Ferrite"
    if any(keyword in text for keyword in ["capacitor", "electronic", "electronics", "pth"]):
        return "Electronic"
    if "brush" in text:
        return "Brush"
    if any(keyword in text for keyword in ["plate", "shield", "lever", "stamping"]):
        return "Stamping"
    if any(keyword in text for keyword in ["plastic", "injection", "bushing"]):
        return "Plastic"
    if "wire" in text:
        return "Enameled wire"

    return "Other"


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if value in [None, ""]:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ["true", "yes", "y", "1", "blocking", "blocked"]:
            return True
        if normalized in ["false", "no", "n", "0"]:
            return False
    return bool(value)


def extract_products(payload):
    products = []
    project_code = normalize_project_code(extract_project_code(payload)) or "UNKNOWN_PROJECT"
    records = as_record_list(payload.get("products_validation"), PRODUCT_LIST_KEYS)

    for product in records:
        product_reference = get_nested_value(product, PRODUCT_REFERENCE_PATHS)
        max_quantity_paths = [
            ["max_quantity"],
            ["maximum_quantity"],
            ["qmax"],
            ["annual_quantity"],
            ["quantity"],
            ["required_information", "max_quantity"],
        ]
        target_price_paths = [
            ["target_price"],
            ["target_price_value"],
            ["price_target"],
            ["commercial", "target_price"],
            ["required_information", "target_price"],
        ]
        sop_paths = [
            ["sop"],
            ["sop_date"],
            ["start_of_production"],
            ["required_information", "sop"],
        ]
        drawing_paths = [
            ["drawing"],
            ["drawing_validation"],
            ["drawing_status"],
            ["required_information", "drawing"],
        ]

        products.append({
            "project_code": normalize_project_code(
                get_nested_value(product, [["project_code"], ["project_number"], ["quote_id"]])
            ) or project_code,
            "product_reference": product_reference,
            "product_line": get_nested_value(product, [
                ["product_line"],
                ["product_type"],
                ["line"],
                ["product", "product_line"],
            ]),
            "product_family": get_nested_value(product, [
                ["product_family"],
                ["family"],
                ["product", "family"],
            ]),
            "drawing_status": get_nested_status(product, drawing_paths),
            "drawing_reference": get_nested_value(product, [
                ["drawing_reference"],
                ["drawing_number"],
                ["plan_reference"],
                ["drawing", "reference"],
                ["drawing", "drawing_reference"],
                ["drawing", "value"],
            ]),
            "max_quantity_status": get_nested_status(product, max_quantity_paths),
            "max_quantity_value": get_nested_value(product, max_quantity_paths),
            "delivery_zone": get_nested_value(product, [
                ["delivery_zone"],
                ["destination_zone"],
                ["zone"],
                ["required_information", "delivery_zone"],
            ]),
            "target_price_status": get_nested_status(product, target_price_paths),
            "target_price_value": get_nested_value(product, target_price_paths),
            "sop_status": get_nested_status(product, sop_paths),
            "sop_value": get_nested_value(product, sop_paths),
            "validation_status": get_nested_status(product, [
                ["validation_status"],
                ["status"],
                ["product_validation_status"],
            ]),
        })

    return products


def extract_components(payload):
    components = []
    project_code = normalize_project_code(extract_project_code(payload)) or "UNKNOWN_PROJECT"
    default_product_reference = extract_product_reference(payload)

    for component in get_component_validation_records(payload.get("components_validation")):
        append_component_rows(
            components=components,
            component=component,
            project_code=project_code,
            inherited_product_reference=default_product_reference,
        )

    for product in as_record_list(payload.get("products_validation"), PRODUCT_LIST_KEYS):
        inherited_product_reference = get_nested_value(product, PRODUCT_REFERENCE_PATHS)
        for component in get_component_validation_records(
            get_dict_value(product, "components_validation") or get_dict_value(product, "components"),
        ):
            append_component_rows(
                components=components,
                component=component,
                project_code=project_code,
                inherited_product_reference=inherited_product_reference,
            )

    return components


def append_component_rows(components, component, project_code, inherited_product_reference):
    parent_product_reference = (
        get_component_parent_product_reference(component)
        or inherited_product_reference
    )
    child_components = get_child_component_records(component)

    if child_components:
        for child_component in child_components:
            components.append(extract_component_row(
                component=child_component,
                project_code=project_code,
                inherited_product_reference=(
                    get_component_parent_product_reference(child_component)
                    or parent_product_reference
                ),
            ))
        return

    components.append(extract_component_row(
        component=component,
        project_code=project_code,
        inherited_product_reference=parent_product_reference,
    ))


def extract_component_row(component, project_code, inherited_product_reference=None):
    quantity_paths = [
        ["quantity"],
        ["quantity_per_product"],
        ["quantity_per_assembly"],
        ["qty"],
        ["required_information", "quantity"],
    ]
    internal_external_paths = [
        ["internal_or_external"],
        ["internal_external"],
        ["make_or_buy"],
        ["sourcing"],
    ]
    component_name = get_nested_value(component, [
        ["component_name"],
        ["name"],
        ["component_description"],
        ["description"],
    ])
    component_code_or_drawing_number = get_nested_value(component, [
        ["component_code_or_drawing_number"],
        ["component_part_number"],
        ["component_code"],
        ["drawing_number"],
        ["drawing_reference"],
        ["part_number"],
    ])
    raw_component_family = get_nested_value(component, [
        ["component_family"],
        ["component_type"],
        ["family"],
        ["technology"],
        ["material_family"],
    ])

    return {
        "project_code": normalize_project_code(
            get_nested_value(component, [["project_code"], ["project_number"], ["quote_id"]])
        ) or project_code,
        "product_reference": get_component_parent_product_reference(component) or inherited_product_reference,
        "component_name": component_name,
        "component_code_or_drawing_number": component_code_or_drawing_number,
        "component_family": raw_component_family or infer_component_family(
            component_name,
            component_code_or_drawing_number,
            json.dumps(component, ensure_ascii=False, default=str),
        ),
        "quantity_status": get_nested_status(component, quantity_paths),
        "quantity_value": get_nested_value(component, quantity_paths),
        "drawing_status": get_nested_status(component, [
            ["drawing"],
            ["drawing_status"],
            ["drawing_validation"],
            ["drawing_availability"],
        ]),
        "plant_to_deliver": get_nested_value(component, [
            ["plant_to_deliver"],
            ["delivery_plant"],
            ["plant"],
            ["site"],
        ]),
        "internal_or_external_status": get_nested_status(component, internal_external_paths),
        "internal_or_external_value": get_nested_value(component, internal_external_paths),
        "validation_status": get_nested_status(component, [
            ["validation_status"],
            ["status"],
            ["component_validation_status"],
        ]),
        "is_blocking": normalize_bool(get_nested_value(component, [
            ["is_blocking"],
            ["blocking"],
        ])),
    }


def extract_validation_issues(payload):
    issues = []

    missing = payload.get("missing_information_summary", [])
    if isinstance(missing, list):
        for item in missing:
            if not isinstance(item, dict):
                continue

            issues.append({
                "level": item.get("level") or "general",
                "field": item.get("field") or item.get("missing_information"),
                "description": item.get("detail") or item.get("reason") or str(item),
                "blocking": bool(item.get("blocking", False)),
            })

    return issues


def save_summary_and_issues(record_id, project_code, product_reference, payload):
    summary = extract_summary(payload)
    customer = extract_customer(payload)
    issues = extract_validation_issues(payload)
    products = extract_products(payload)
    components = extract_components(payload)

    blocking_count = sum(1 for i in issues if i.get("blocking"))

    with db_connect() as conn:
        with conn.cursor() as cur:
            for table in [
                "rfq_validation_summary",
                "rfq_validation_issues",
                "rfq_validation_products",
                "rfq_validation_components",
            ]:
                cur.execute(
                    f"DELETE FROM {table} WHERE agent_json_record_id = %s",
                    (record_id,),
                )

            cur.execute("""
                INSERT INTO rfq_validation_summary
                (
                    agent_json_record_id,
                    project_code,
                    customer,
                    product_reference,
                    package_status,
                    can_continue_to_costing,
                    missing_count,
                    blocking_missing_count,
                    next_agent,
                    blocking_reason
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                record_id,
                project_code,
                customer,
                product_reference,
                summary.get("package_status"),
                summary.get("can_continue_to_costing"),
                summary.get("missing_count"),
                blocking_count,
                summary.get("next_agent"),
                summary.get("reason"),
            ))

            for issue in issues:
                cur.execute("""
                    INSERT INTO rfq_validation_issues
                    (
                        agent_json_record_id,
                        project_code,
                        product_reference,
                        issue_level,
                        issue_field,
                        issue_description,
                        is_blocking
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    record_id,
                    project_code,
                    product_reference,
                    issue.get("level"),
                    issue.get("field"),
                    issue.get("description"),
                    issue.get("blocking"),
                ))

            for product in products:
                cur.execute("""
                    INSERT INTO rfq_validation_products
                    (
                        agent_json_record_id,
                        project_code,
                        product_reference,
                        product_line,
                        product_family,
                        drawing_status,
                        drawing_reference,
                        max_quantity_status,
                        max_quantity_value,
                        delivery_zone,
                        target_price_status,
                        target_price_value,
                        sop_status,
                        sop_value,
                        validation_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    record_id,
                    product.get("project_code") or project_code,
                    product.get("product_reference"),
                    product.get("product_line"),
                    product.get("product_family"),
                    product.get("drawing_status"),
                    product.get("drawing_reference"),
                    product.get("max_quantity_status"),
                    product.get("max_quantity_value"),
                    product.get("delivery_zone"),
                    product.get("target_price_status"),
                    product.get("target_price_value"),
                    product.get("sop_status"),
                    product.get("sop_value"),
                    product.get("validation_status"),
                ))

            for component in components:
                cur.execute("""
                    INSERT INTO rfq_validation_components
                    (
                        agent_json_record_id,
                        project_code,
                        product_reference,
                        component_name,
                        component_code_or_drawing_number,
                        component_family,
                        quantity_status,
                        quantity_value,
                        drawing_status,
                        plant_to_deliver,
                        internal_or_external_status,
                        internal_or_external_value,
                        validation_status,
                        is_blocking
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    record_id,
                    component.get("project_code") or project_code,
                    component.get("product_reference"),
                    component.get("component_name"),
                    component.get("component_code_or_drawing_number"),
                    component.get("component_family"),
                    component.get("quantity_status"),
                    component.get("quantity_value"),
                    component.get("drawing_status"),
                    component.get("plant_to_deliver"),
                    component.get("internal_or_external_status"),
                    component.get("internal_or_external_value"),
                    component.get("validation_status"),
                    component.get("is_blocking"),
                ))

        conn.commit()
def extract_customer(payload):
    return get_nested_value(payload, [
        ["project_validation", "customer"],
        ["project_validation", "required_information", "customer"],
        ["project", "customer"],
    ])


def extract_product_reference(payload):
    products = as_record_list(payload.get("products_validation"), PRODUCT_LIST_KEYS)
    if len(products) == 1:
        product = products[0]
        return get_nested_value(product, PRODUCT_REFERENCE_PATHS)
    return None
def save_record(project_code, product_reference, json_type, source_agent, validation_status, payload):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_json_records
                (
                    project_code,
                    product_reference,
                    json_type,
                    source_agent,
                    validation_status,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING agent_json_record_id
            """, (
                project_code,
                product_reference,
                json_type,
                source_agent,
                validation_status,
                json.dumps(payload, ensure_ascii=False),
            ))
            record_id = cur.fetchone()["agent_json_record_id"]
        conn.commit()
    return record_id


def text_contains(text, keywords):
    normalized = str(text or "").lower()
    return any(keyword in normalized for keyword in keywords)


def component_search_text(component):
    inferred_family = component.get("component_family") or infer_component_family(
        component.get("component_name"),
        component.get("component_code_or_drawing_number"),
    )

    return " ".join(
        str(component.get(field) or "")
        for field in [
            "component_name",
            "component_code_or_drawing_number",
            "internal_or_external_status",
            "internal_or_external_value",
            "validation_status",
        ]
    ).lower() + f" {inferred_family.lower()}"


def value_indicates_internal(value):
    return text_contains(
        value,
        ["internal", "interne", "in house", "in-house", "make", "avocarbon"],
    )


def value_indicates_external(value):
    return text_contains(
        value,
        ["external", "externe", "supplier", "buy", "purchase", "purchased", "outsourced"],
    )


def component_blocking_reasons(component):
    reasons = []

    validation_status = str(component.get("validation_status") or "").lower()
    if "blocked" in validation_status or "blocking" in validation_status:
        reasons.append(f"validation_status is {component.get('validation_status')}")

    component_name = str(component.get("component_name") or "").lower()
    if "component list not confirmed" in component_name:
        reasons.append("component list not confirmed")

    if not component.get("component_code_or_drawing_number"):
        reasons.append("component_code_or_drawing_number is missing")

    if not component.get("quantity_value") and not component.get("max_quantity_value"):
        reasons.append("quantity_value and max_quantity_per_year are missing")

    return list(dict.fromkeys(reasons))


def component_orchestration_notes(component):
    notes = []

    if not component.get("drawing_status") and component.get("component_code_or_drawing_number"):
        notes.append("component drawing not confirmed")

    return notes


def classify_component_costing_route(component):
    search_text = component_search_text(component)
    component_family = component.get("component_family") or infer_component_family(
        component.get("component_name"),
        component.get("component_code_or_drawing_number"),
    )
    blocking_reasons = component_blocking_reasons(component)
    notes = component_orchestration_notes(component)
    note_text = "; ".join(notes) if notes else None

    if blocking_reasons:
        return {
            "costing_route": "blocked_missing_data",
            "required_agent": None,
            "blocking_reason": "; ".join(blocking_reasons),
            "component_family": component_family,
            "notes": notes,
        }

    if component_family == "Spring" or text_contains(search_text, ["spring"]):
        return {
            "costing_route": "automatic_external_costing",
            "required_agent": "Spring costing agent",
            "blocking_reason": note_text,
            "component_family": component_family,
            "notes": notes,
        }

    if component_family == "Choke/Ferrite" or text_contains(search_text, ["ferrite", "choke", "inductor"]):
        return {
            "costing_route": "automatic_external_costing",
            "required_agent": "Ferrite/Choke costing agent",
            "component_family": component_family,
            "blocking_reason": note_text,
            "notes": notes,
        }

    if component_family == "Electronic" or text_contains(search_text, ["electronic", "electronics", "capacitor", "pth"]):
        return {
            "costing_route": "automatic_external_costing",
            "required_agent": "Electronic/PTH costing agent",
            "blocking_reason": note_text,
            "component_family": component_family,
            "notes": notes,
        }

    if component_family == "Brush" or text_contains(search_text, ["brush"]):
        return {
            "costing_route": "brush_costing_agent_required",
            "required_agent": "Brush costing agent",
            "blocking_reason": note_text,
            "component_family": component_family,
            "notes": notes,
        }

    if component_family == "Stamping" or text_contains(search_text, ["plate", "shield", "lever", "stamping"]):
        return {
            "costing_route": "automatic_external_costing",
            "required_agent": "Stamping costing agent",
            "blocking_reason": note_text,
            "component_family": component_family,
            "notes": notes,
        }

    if component_family == "Plastic" or text_contains(search_text, ["plastic", "injection", "bushing"]):
        return {
            "costing_route": "automatic_external_costing",
            "required_agent": "Plastic costing agent / material provider required",
            "component_family": component_family,
            "blocking_reason": note_text,
            "notes": notes,
        }

    if component_family == "Enameled wire" or text_contains(search_text, ["enameled wire", "enamelled wire", "wire"]):
        return {
            "costing_route": "automatic_external_costing",
            "required_agent": "Enameled wire costing agent",
            "blocking_reason": note_text,
            "component_family": component_family,
            "notes": notes,
        }

    return {
        "costing_route": "manual_costing_engineer_required",
        "required_agent": None,
        "blocking_reason": note_text or "No automatic costing route matched this component.",
        "component_family": component_family,
        "notes": notes,
    }


def load_component_project_codes():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT project_code
                FROM rfq_validation_components
                WHERE project_code IS NOT NULL
                ORDER BY project_code DESC
            """)
            return [row["project_code"] for row in cur.fetchall()]


def load_latest_validation_components(project_code):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(agent_json_record_id) AS latest_record_id
                FROM rfq_validation_components
                WHERE project_code = %s
            """, (project_code,))
            latest_record = cur.fetchone()
            latest_record_id = latest_record["latest_record_id"] if latest_record else None

            if not latest_record_id:
                return []

            cur.execute("""
                SELECT
                    c.rfq_validation_component_id,
                    c.agent_json_record_id,
                    c.project_code,
                    COALESCE(c.product_reference, sp.product_reference) AS product_reference,
                    c.component_name,
                    c.component_code_or_drawing_number,
                    c.component_family,
                    c.quantity_status,
                    c.quantity_value,
                    c.drawing_status,
                    c.plant_to_deliver,
                    c.internal_or_external_status,
                    c.internal_or_external_value,
                    c.validation_status,
                    c.is_blocking,
                    c.created_at,
                    COALESCE(p.max_quantity_value, sp.max_quantity_value) AS max_quantity_value
                FROM rfq_validation_components c
                LEFT JOIN rfq_validation_products p
                    ON p.agent_json_record_id = c.agent_json_record_id
                    AND p.project_code = c.project_code
                    AND p.product_reference = c.product_reference
                LEFT JOIN (
                    SELECT
                        agent_json_record_id,
                        project_code,
                        MAX(product_reference) AS product_reference,
                        MAX(max_quantity_value) AS max_quantity_value
                    FROM rfq_validation_products
                    WHERE agent_json_record_id = %s
                        AND project_code = %s
                    GROUP BY agent_json_record_id, project_code
                    HAVING COUNT(*) = 1
                ) sp
                    ON sp.agent_json_record_id = c.agent_json_record_id
                    AND sp.project_code = c.project_code
                WHERE c.project_code = %s
                    AND c.agent_json_record_id = %s
                ORDER BY c.is_blocking DESC, c.rfq_validation_component_id
            """, (latest_record_id, project_code, project_code, latest_record_id))
            return cur.fetchall()


def generate_component_costing_queue(project_code):
    components = load_latest_validation_components(project_code)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM component_costing_queue WHERE project_code = %s",
                (project_code,),
            )

            for component in components:
                classification = classify_component_costing_route(component)
                component_family = (
                    classification.get("component_family")
                    or component.get("component_family")
                    or infer_component_family(
                        component.get("component_name"),
                        component.get("component_code_or_drawing_number"),
                    )
                )
                component_json = dict(component)
                component_json["component_family"] = component_family
                if classification.get("notes"):
                    component_json["orchestration_notes"] = classification["notes"]
                cur.execute("""
                    INSERT INTO component_costing_queue
                    (
                        source_validation_component_id,
                        agent_json_record_id,
                        project_code,
                        product_reference,
                        component_name,
                        component_code_or_drawing_number,
                        component_family,
                        costing_route,
                        costing_status,
                        required_agent,
                        blocking_reason,
                        country_to_produce,
                        max_quantity_per_year,
                        component_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """, (
                    component.get("rfq_validation_component_id"),
                    component.get("agent_json_record_id"),
                    component.get("project_code"),
                    component.get("product_reference"),
                    component.get("component_name"),
                    component.get("component_code_or_drawing_number"),
                    component_family,
                    classification.get("costing_route"),
                    "pending",
                    classification.get("required_agent"),
                    classification.get("blocking_reason"),
                    component.get("plant_to_deliver"),
                    component.get("max_quantity_value") or component.get("quantity_value"),
                    json.dumps(component_json, ensure_ascii=False, default=str),
                ))

        conn.commit()

    return len(components)


def load_component_costing_queue(project_code=None):
    with db_connect() as conn:
        with conn.cursor() as cur:
            if project_code:
                cur.execute("""
                    SELECT
                        project_code,
                        product_reference,
                        component_name,
                        component_code_or_drawing_number,
                        component_family,
                        costing_route,
                        costing_status,
                        required_agent,
                        blocking_reason,
                        country_to_produce,
                        max_quantity_per_year,
                        created_at
                    FROM component_costing_queue
                    WHERE project_code = %s
                    ORDER BY created_at DESC, component_costing_queue_id DESC
                """, (project_code,))
            else:
                cur.execute("""
                    SELECT
                        project_code,
                        product_reference,
                        component_name,
                        component_code_or_drawing_number,
                        component_family,
                        costing_route,
                        costing_status,
                        required_agent,
                        blocking_reason,
                        country_to_produce,
                        max_quantity_per_year,
                        created_at
                    FROM component_costing_queue
                    ORDER BY created_at DESC, component_costing_queue_id DESC
                    LIMIT 100
                """)

            return cur.fetchall()


def summarize_component_costing_queue(queue_rows):
    summary = {
        "total_components": len(queue_rows),
        "automatic_external_costing": 0,
        "internal_costing_required": 0,
        "manual_costing_engineer_required": 0,
        "blocked_missing_data": 0,
    }

    for row in queue_rows:
        route = row.get("costing_route")

        if route == "automatic_external_costing" or route == "brush_costing_agent_required":
            summary["automatic_external_costing"] += 1
        elif route == "internal_costing_required":
            summary["internal_costing_required"] += 1
        elif route == "manual_costing_engineer_required":
            summary["manual_costing_engineer_required"] += 1
        elif route == "blocked_missing_data":
            summary["blocked_missing_data"] += 1

    return summary


DEFAULT_TEST_SCENARIOS = [
    {
        "scenario_name": "Complete simple external spring",
        "scenario_type": "component_costing_route",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Validated component with product reference, component code, quantity, "
            "external sourcing, and family/name containing spring."
        ),
        "expected_output": (
            "Queue row is automatic_external_costing with required_agent = "
            "Spring costing agent and no blocking reason."
        ),
    },
    {
        "scenario_name": "External electronic/PTH component",
        "scenario_type": "component_costing_route",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Validated external component with component family or name containing "
            "electronic, capacitor, or PTH."
        ),
        "expected_output": (
            "Queue row is automatic_external_costing with required_agent = "
            "Electronic/PTH costing agent."
        ),
    },
    {
        "scenario_name": "External ferrite/choke",
        "scenario_type": "component_costing_route",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Validated external component with family/name containing ferrite, "
            "choke, or inductor."
        ),
        "expected_output": (
            "Queue row is automatic_external_costing with required_agent = "
            "Ferrite/Choke costing agent."
        ),
    },
    {
        "scenario_name": "External enameled wire",
        "scenario_type": "component_costing_route",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Validated external component with family/name containing enameled "
            "wire, enamelled wire, or wire."
        ),
        "expected_output": (
            "Queue row is automatic_external_costing with required_agent = "
            "Enameled wire costing agent."
        ),
    },
    {
        "scenario_name": "Plastic part requiring material provider",
        "scenario_type": "component_costing_route",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Validated external plastic, injection, or bushing component with "
            "component code and quantity."
        ),
        "expected_output": (
            "Queue row is automatic_external_costing with required_agent = "
            "Plastic costing agent / material provider required."
        ),
    },
    {
        "scenario_name": "Internal component sent to external costing agent",
        "scenario_type": "routing_exception",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Component validation indicates internal sourcing but the package "
            "also contains external-costing keywords."
        ),
        "expected_output": (
            "Scenario should be reviewed. Current orchestrator should not send "
            "internal components directly to an external costing agent without "
            "business validation."
        ),
    },
    {
        "scenario_name": "Unknown unsupported component",
        "scenario_type": "component_costing_route",
        "agent_under_test": "Component Costing Orchestrator",
        "input_required": (
            "Validated component with code and quantity but no recognized family "
            "or keyword."
        ),
        "expected_output": (
            "Queue row is manual_costing_engineer_required with a clear reason."
        ),
    },
    {
        "scenario_name": "Missing annual quantity",
        "scenario_type": "rfq_validation",
        "agent_under_test": "Manual RFQ validation agent",
        "input_required": (
            "RFQ package where project/product information is present but max "
            "quantity per year or annual quantity is missing."
        ),
        "expected_output": (
            "Validation JSON flags missing quantity. Component costing queue is "
            "blocked only if no component quantity and no product max quantity "
            "are available."
        ),
    },
    {
        "scenario_name": "Assembly with incomplete BOM",
        "scenario_type": "rfq_validation",
        "agent_under_test": "Manual RFQ validation agent",
        "input_required": (
            "Assembly package where component list is incomplete or contains "
            "'component list not confirmed'."
        ),
        "expected_output": (
            "Validation issues include incomplete BOM. Component costing queue "
            "rows are blocked_missing_data for unconfirmed component lists."
        ),
    },
    {
        "scenario_name": "Assembly with complete BOM",
        "scenario_type": "rfq_validation_to_orchestration",
        "agent_under_test": "Manual RFQ validation agent + Component Costing Orchestrator",
        "input_required": (
            "Assembly package with product reference, complete BOM, component "
            "codes, quantities, and validated component families."
        ),
        "expected_output": (
            "Detected components retain product_reference, component family, "
            "quantity, and route to the correct costing agent or manual queue."
        ),
    },
]


def seed_default_test_scenarios():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS scenario_count FROM agent_test_scenarios")
            scenario_count = cur.fetchone()["scenario_count"]

            if scenario_count:
                return

            for scenario in DEFAULT_TEST_SCENARIOS:
                cur.execute("""
                    INSERT INTO agent_test_scenarios
                    (
                        scenario_name,
                        scenario_type,
                        agent_under_test,
                        input_required,
                        expected_output
                    )
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    scenario["scenario_name"],
                    scenario["scenario_type"],
                    scenario["agent_under_test"],
                    scenario["input_required"],
                    scenario["expected_output"],
                ))

        conn.commit()


def load_test_scenarios():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    scenario_id,
                    scenario_name,
                    scenario_type,
                    agent_under_test,
                    input_required,
                    expected_output,
                    actual_output,
                    test_status,
                    notes,
                    created_at,
                    updated_at
                FROM agent_test_scenarios
                ORDER BY scenario_id
            """)
            return cur.fetchall()


def update_test_scenario_result(scenario_id, actual_output, test_status, notes):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE agent_test_scenarios
                SET
                    actual_output = %s,
                    test_status = %s,
                    notes = %s,
                    updated_at = NOW()
                WHERE scenario_id = %s
            """, (
                actual_output,
                test_status,
                notes,
                scenario_id,
            ))
        conn.commit()


def get_first_available_value(records, paths):
    for record in records:
        if not isinstance(record, dict):
            continue

        value = get_nested_value(record, paths)
        if field_has_value(value):
            return value

    return None


def find_value_by_keys(data, candidate_keys, max_depth=6):
    if max_depth < 0:
        return None

    if isinstance(data, dict):
        for key in candidate_keys:
            value = get_dict_value(data, key)
            unwrapped = unwrap_validation_value(value)
            if field_has_value(unwrapped):
                return unwrapped

        for value in data.values():
            found = find_value_by_keys(value, candidate_keys, max_depth - 1)
            if field_has_value(found):
                return found

    if isinstance(data, list):
        for item in data:
            found = find_value_by_keys(item, candidate_keys, max_depth - 1)
            if field_has_value(found):
                return found

    return None


def coerce_number(value):
    if isinstance(value, bool) or value in [None, ""]:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value) if value.is_integer() else value

    text = str(value).strip()
    if not text:
        return None

    cleaned = re.sub(r"[^0-9,.\-]", "", text)
    if not cleaned or cleaned in ["-", ".", ","]:
        return value

    if cleaned.count(",") > 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")

    try:
        number = float(cleaned)
    except ValueError:
        return value

    return int(number) if number.is_integer() else number


def add_unique_text(values, value):
    if isinstance(value, list):
        for item in value:
            add_unique_text(values, item)
        return

    if isinstance(value, dict):
        value = unwrap_validation_value(value)

    if not field_has_value(value):
        return

    text = str(value).strip()
    if text and text not in values:
        values.append(text)


def get_costing_product_records(rfq_data):
    products = extract_rfq_products(rfq_data)
    if products:
        return products

    return as_record_list(get_dict_value(rfq_data, "products_validation"), PRODUCT_LIST_KEYS)


def get_costing_component_records(rfq_data):
    components = []

    for key in [
        "components_validation",
        "components",
        "component_list",
        "bill_of_materials",
        "bom",
        "bom_lines",
    ]:
        value = get_dict_value(rfq_data, key)

        for record in get_component_validation_records(value):
            child_components = get_child_component_records(record)
            parent_reference = get_component_parent_product_reference(record)

            if child_components:
                for child in child_components:
                    child_row = dict(child)
                    if parent_reference and not get_component_parent_product_reference(child_row):
                        child_row["product_reference"] = parent_reference
                    components.append(child_row)
            else:
                components.append(record)

    deduped = []
    seen = set()
    for component in components:
        key = json.dumps(component, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            deduped.append(component)

    return deduped


def extract_part_numbers_from_rfq_data(rfq_data):
    part_numbers = []
    products = get_costing_product_records(rfq_data)
    components = get_costing_component_records(rfq_data)

    for key in ["part_numbers", "part_number_list", "customer_part_numbers"]:
        add_unique_text(part_numbers, get_dict_value(rfq_data, key))

    part_number_paths = [
        ["part_number"],
        ["customer_part_number"],
        ["customer_reference"],
        ["product_reference"],
        ["reference"],
    ]
    component_number_paths = [
        ["component_part_number"],
        ["component_code"],
        ["component_code_or_drawing_number"],
        ["drawing_number"],
        ["part_number"],
        ["reference"],
    ]

    for product in products:
        add_unique_text(part_numbers, get_nested_value(product, part_number_paths))

    for component in components:
        add_unique_text(part_numbers, get_nested_value(component, component_number_paths))

    return part_numbers


def extract_technical_data_from_rfq_data(rfq_data):
    technical_data = {}
    sources = [rfq_data] + get_costing_product_records(rfq_data) + get_costing_component_records(rfq_data)
    technical_fields = {
        "wire_diameter_mm": [
            "wire_diameter_mm",
            "wire_diameter",
            "diameter_wire_mm",
            "wire_size_mm",
        ],
        "turns": ["turns", "number_of_turns", "turn_count"],
        "inductance_uH": ["inductance_uH", "inductance_uh", "inductance"],
        "loss": ["loss", "attenuation", "insertion_loss"],
        "core_length_mm": ["core_length_mm", "ferrite_core_length_mm"],
        "core_diameter_mm": ["core_diameter_mm", "ferrite_core_diameter_mm"],
    }

    for output_key, candidate_keys in technical_fields.items():
        value = None
        for source in sources:
            value = find_value_by_keys(source, candidate_keys)
            if field_has_value(value):
                break

        if field_has_value(value):
            if output_key != "loss":
                value = coerce_number(value)
            technical_data[output_key] = value

    return technical_data


def extract_commercial_data_from_rfq_data(rfq_data):
    products = get_costing_product_records(rfq_data)
    sources = [rfq_data] + products
    commercial_fields = {
        "target_price": ["target_price", "target_price_value", "price_target"],
        "currency": ["currency", "target_price_currency", "price_currency"],
        "delivery_condition": [
            "delivery_condition",
            "delivery_conditions",
            "expected_delivery_conditions",
            "incoterm",
        ],
        "payment_terms": [
            "payment_terms",
            "payment_conditions",
            "expected_payment_terms",
        ],
    }
    commercial_data = {}

    for output_key, candidate_keys in commercial_fields.items():
        value = None
        for source in sources:
            value = find_value_by_keys(source, candidate_keys)
            if field_has_value(value):
                break

        if field_has_value(value):
            if output_key == "target_price":
                value = coerce_number(value)
            commercial_data[output_key] = value

    return commercial_data


def normalize_component_family_for_agent(component_family):
    if not component_family:
        return None

    normalized = str(component_family).strip().lower()
    if "choke" in normalized or "ferrite" in normalized or "inductor" in normalized:
        return "ferrite/choke"
    if "enameled" in normalized or "wire" in normalized:
        return "enameled wire"

    return normalized


def derive_production_country(rfq_data, destination_zone, components):
    production_country = get_first_available_value([rfq_data], [
        ["production_country"],
        ["country_to_produce"],
        ["manufacturing_country"],
        ["plant_to_deliver"],
    ])

    if not field_has_value(production_country):
        production_country = get_first_available_value(components, [
            ["country_to_produce"],
            ["production_country"],
            ["plant_to_deliver"],
        ])

    if field_has_value(production_country):
        return production_country

    if destination_zone and "china" in str(destination_zone).lower():
        return "China"

    return None


def parse_number_from_costing_text(pattern, text):
    match = re.search(pattern, text or "", re.IGNORECASE)
    if not match:
        return None

    return coerce_number(match.group(1))


def parse_costing_data_technical_specification(costing_data):
    technical_specification = {}

    if not field_has_value(costing_data):
        return technical_specification

    if isinstance(costing_data, dict):
        technical_specification["raw_costing_data"] = costing_data
        field_map = {
            "wire_diameter_mm": [
                ["wire_diameter_mm"],
                ["wire_diameter"],
                ["diameter_wire_mm"],
            ],
            "number_of_turns": [
                ["number_of_turns"],
                ["turns"],
                ["turn_count"],
            ],
            "inductance_uH": [
                ["inductance_uH"],
                ["inductance_uh"],
                ["inductance"],
            ],
            "loss": [["loss"], ["attenuation"], ["insertion_loss"]],
            "core_length_mm": [["core_length_mm"], ["core_length"]],
            "core_diameter_mm": [["core_diameter_mm"], ["core_diameter"]],
        }

        for output_key, paths in field_map.items():
            value = get_nested_value(costing_data, paths)
            if field_has_value(value):
                technical_specification[output_key] = (
                    coerce_number(value) if output_key != "loss" else value
                )

        return technical_specification

    raw_text = str(costing_data).strip()
    if not raw_text:
        return technical_specification

    technical_specification["raw_costing_data"] = raw_text
    technical_specification["wire_diameter_mm"] = parse_number_from_costing_text(
        r"wire\s*diameter\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)",
        raw_text,
    )
    technical_specification["number_of_turns"] = parse_number_from_costing_text(
        r"(?:number\s+of\s+turns|turns)\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)",
        raw_text,
    )
    technical_specification["inductance_uH"] = parse_number_from_costing_text(
        r"inductance\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)",
        raw_text,
    )
    technical_specification["core_length_mm"] = parse_number_from_costing_text(
        r"core\s*length\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)",
        raw_text,
    )
    technical_specification["core_diameter_mm"] = parse_number_from_costing_text(
        r"core\s*diameter\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)",
        raw_text,
    )

    loss_match = re.search(
        r"loss\s*[:=]\s*(.*?)(?=;\s*(?:core\s*length|core\s*diameter)\s*[:=]|$)",
        raw_text,
        re.IGNORECASE,
    )
    if loss_match:
        technical_specification["loss"] = loss_match.group(1).strip(" ;")

    return {
        key: value
        for key, value in technical_specification.items()
        if field_has_value(value)
    }


def build_external_technical_specification(rfq_data):
    costing_data = get_first_available_value([rfq_data], [
        ["costing_data"],
        ["technical_specification"],
        ["technical_data"],
        ["technical"],
    ])
    technical_specification = parse_costing_data_technical_specification(costing_data)

    fallback_map = {
        "wire_diameter_mm": [
            "wire_diameter_mm",
            "wire_diameter",
            "diameter_wire_mm",
        ],
        "number_of_turns": ["number_of_turns", "turns", "turn_count"],
        "inductance_uH": ["inductance_uH", "inductance_uh", "inductance"],
        "loss": ["loss", "attenuation", "insertion_loss"],
        "core_length_mm": ["core_length_mm", "core_length"],
        "core_diameter_mm": ["core_diameter_mm", "core_diameter"],
    }

    for output_key, candidate_keys in fallback_map.items():
        if field_has_value(technical_specification.get(output_key)):
            continue

        value = find_value_by_keys(rfq_data, candidate_keys)
        if field_has_value(value):
            technical_specification[output_key] = (
                coerce_number(value) if output_key != "loss" else value
            )

    return technical_specification


def resolve_product_line_for_external_payload(rfq_data):
    acronym = get_first_available_value([rfq_data], [
        ["product_line_acronym"],
        ["product_line_code"],
        ["product_acronym"],
    ])
    product_line = normalize_product_line(acronym) or normalize_product_line(
        get_first_available_value([rfq_data], [["product_line"], ["product_type"]])
    )

    if field_has_value(product_line):
        return product_line

    return infer_product_line_from_rfq_id(
        get_first_available_value([rfq_data], [
            ["systematic_rfq_id"],
            ["systematicRfqId"],
            ["project_number"],
            ["project_code"],
        ])
    )


def extract_external_component_name(rfq_data):
    return get_first_available_value([rfq_data], [
        ["component_name"],
        ["product_name"],
        ["product"],
        ["name"],
    ])


def extract_external_component_reference(rfq_data):
    components = get_costing_component_records(rfq_data)
    first_component = components[0] if components else {}

    return get_first_available_value([first_component, rfq_data], [
        ["component_reference"],
        ["component_part_number"],
        ["component_code_or_drawing_number"],
        ["component_code"],
        ["drawing_number"],
        ["drawing_reference"],
    ])


def infer_supported_external_component_family(rfq_data, product_line):
    explicit_family = get_first_available_value([rfq_data], [
        ["component_family"],
        ["component_type"],
        ["family"],
    ])
    component_name = extract_external_component_name(rfq_data)
    search_text = " ".join([
        str(explicit_family or ""),
        str(component_name or ""),
    ]).lower()

    if "busbar" in search_text or "bus bar" in search_text:
        return "Stamped part / copper busbar", "busbar"
    if any(keyword in search_text for keyword in ["stamped", "stamping", "copper stamped"]):
        return "Stamped part", "stamping"
    if any(keyword in search_text for keyword in ["choke", "rod choke", "inductor"]):
        return "Ferrite / Choke", "choke"
    if "ferrite" in search_text:
        return "Ferrite material/component", "ferrite"
    if any(keyword in search_text for keyword in ["electronic", "electronics", "capacitor", "pth"]):
        return "Electronic / PTH", "electronic"
    if "spring" in search_text:
        return "Spring", "spring"
    if any(keyword in search_text for keyword in ["plastic", "injection", "bushing"]):
        return "Plastic", "plastic"
    if "wire" in search_text:
        return "Enameled wire", "wire"

    if normalize_lookup_key(product_line) == "assembly":
        return None, None

    inferred_family = infer_component_family(component_name, explicit_family, product_line)
    return inferred_family, normalize_lookup_key(inferred_family)


def resolve_external_component_family(rfq_data):
    product_line = resolve_product_line_for_external_payload(rfq_data)
    normalized = normalize_lookup_key(product_line)
    supported_component_family, supported_key = infer_supported_external_component_family(
        rfq_data,
        product_line,
    )

    if normalized == "assembly":
        return {
            "product_family": "Assembly",
            "component_family": supported_component_family,
            "component_family_key": supported_key,
            "is_assembly_rfq": True,
            "routing_reason": (
                "Assembly RFQ requires component-level routing before external costing"
                if not supported_component_family
                else f"{supported_component_family} identified inside assembly RFQ"
            ),
        }

    mapping = {
        "chokes": {
            "product_family": "Choke",
            "component_family": "Ferrite / Choke",
            "component_family_key": "choke",
            "is_assembly_rfq": False,
            "routing_reason": "Rod choke / ferrite-based magnetic component",
        },
        "brushes": {
            "product_family": "Brush",
            "component_family": "Brush / internal or brush costing required",
            "component_family_key": "brush",
            "is_assembly_rfq": False,
            "routing_reason": "Brush requires internal or brush-specific costing route",
        },
        "seals": {
            "product_family": "Seal",
            "component_family": "Seal / outside external component scope unless defined",
            "component_family_key": "seal",
            "is_assembly_rfq": False,
            "routing_reason": "Seal is outside external component scope unless defined",
        },
        "advancedmaterial": {
            "product_family": "Advanced Material",
            "component_family": "Advanced Material",
            "component_family_key": "advancedmaterial",
            "is_assembly_rfq": False,
            "routing_reason": "Advanced material component routed by material expertise",
        },
        "friction": {
            "product_family": "Friction",
            "component_family": "Friction",
            "component_family_key": "friction",
            "is_assembly_rfq": False,
            "routing_reason": "Friction component routed by friction expertise",
        },
    }

    if normalized in mapping:
        return mapping[normalized]

    inferred_family = infer_component_family(
        get_first_available_value([rfq_data], [["product_name"], ["component_name"]]),
        product_line,
    )
    return {
        "product_family": product_type_family_from_product_line(product_line) or inferred_family,
        "component_family": inferred_family,
        "component_family_key": normalize_lookup_key(inferred_family),
        "is_assembly_rfq": False,
        "routing_reason": "Costing-ready external component",
    }


def extract_annual_max_quantity(rfq_data, products):
    quantity_values = [
        get_first_available_value([rfq_data], [
            ["annual_max_quantity"],
            ["annual_volume"],
            ["max_quantity"],
            ["qmax"],
            ["quantity"],
        ])
    ]

    for product in products:
        quantity_values.append(get_first_available_value([product], [
            ["quantity"],
            ["annual_volume"],
            ["annual_quantity"],
            ["max_quantity"],
            ["qmax"],
        ]))

    numeric_values = []
    first_present_value = None
    for value in quantity_values:
        if not field_has_value(value):
            continue
        if first_present_value is None:
            first_present_value = value

        coerced = coerce_number(value)
        if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
            numeric_values.append(coerced)

    if numeric_values:
        annual_max_quantity = max(numeric_values)
        return int(annual_max_quantity) if float(annual_max_quantity).is_integer() else annual_max_quantity

    return first_present_value


def normalize_evidence_token(value):
    if not field_has_value(value):
        return None

    normalized = re.sub(r"[^a-z0-9]+", "", str(value).lower())
    return normalized or None


def document_names(documents):
    names = []
    for document in documents:
        name = get_first_available_value([document], [
            ["file_name"],
            ["filename"],
            ["name"],
            ["url"],
            ["download_url"],
        ])
        if field_has_value(name):
            names.append(str(name))

    return names


def document_mentions_component(documents, component_name, component_reference):
    names = document_names(documents)
    if not names:
        return False

    evidence_tokens = []
    component_text = str(component_name or "").lower()
    if "busbar" in component_text or "bus bar" in component_text:
        evidence_tokens.append("busbar")

    component_reference_token = normalize_evidence_token(component_reference)
    if component_reference_token:
        evidence_tokens.append(component_reference_token)

    if not evidence_tokens:
        return False

    for name in names:
        normalized_name = normalize_evidence_token(name)
        if not normalized_name:
            continue

        if any(token and token in normalized_name for token in evidence_tokens):
            return True

    return False


def detect_document_component_mismatch(documents, component_name, component_family):
    names_text = " ".join(document_names(documents)).lower()
    if not names_text:
        return False

    component_text = " ".join([
        str(component_name or ""),
        str(component_family or ""),
    ]).lower()
    mismatch_terms = ["carbon brush", "brush", "braid", "shunt"]

    return any(term in names_text for term in mismatch_terms) and not any(
        term in component_text for term in mismatch_terms
    )


CHOKE_DECOMPOSITION_WORKFLOW_STEPS = [
    "define_ferrite_and_quantity",
    "define_wire_and_quantity",
    "define_tin_and_quantity",
    "cost_external_components",
    "calculate_added_value",
    "apply_plant_manufacturing_cost",
    "calculate_full_pnl",
    "run_npv_balancing_macro",
]

CHOKE_DECOMPOSITION_ROUTING_REASON = (
    "Choke must be decomposed into ferrite, wire, tin, added value, "
    "plant cost, P&L and NPV before final costing."
)


def is_complete_choke_context(rfq_data, family_info, component_name):
    product_line = resolve_product_line_for_external_payload(rfq_data)
    product_line_acronym = get_first_available_value([rfq_data], [
        ["product_line_acronym"],
        ["product_line_code"],
        ["product_acronym"],
    ])
    context_text = " ".join([
        str(product_line_acronym or ""),
        str(product_line or ""),
        str(family_info.get("product_family") or ""),
        str(family_info.get("component_family") or ""),
        str(component_name or ""),
    ]).lower()

    return (
        normalize_lookup_key(product_line_acronym) == "cho"
        or normalize_lookup_key(product_line) == "chokes"
        or "rod choke" in context_text
        or "ferrite / choke" in context_text
        or "ferrite/choke" in context_text
        or "choke" in context_text
    )


def build_external_component_costing_payload_from_rfq_data(rfq_data):
    products = get_costing_product_records(rfq_data)
    family_info = resolve_external_component_family(rfq_data)
    documents = extract_rfq_files(rfq_data)
    technical_specification = build_external_technical_specification(rfq_data)
    project_number = normalize_project_code(get_first_available_value([rfq_data], [
        ["systematic_rfq_id"],
        ["systematicRfqId"],
        ["project_number"],
        ["project_code"],
        ["rfq_id"],
    ]))
    component_name = extract_external_component_name(rfq_data)
    component_reference = extract_external_component_reference(rfq_data)
    part_numbers = []

    for product in products:
        add_unique_text(part_numbers, get_nested_value(product, [
            ["part_number"],
            ["customer_part_number"],
            ["product_reference"],
        ]))

    annual_max_quantity = extract_annual_max_quantity(rfq_data, products)
    document_matches_component = document_mentions_component(
        documents,
        component_name,
        component_reference,
    )
    document_component_mismatch = detect_document_component_mismatch(
        documents,
        component_name,
        family_info["component_family"],
    )
    commercial_data = {
        "target_price": coerce_number(get_first_available_value([rfq_data], [
            ["target_price_eur"],
            ["target_price_local"],
            ["target_price"],
            ["target_price_value"],
        ])),
        "currency": get_first_available_value([rfq_data], [
            ["target_price_currency"],
            ["currency"],
            ["price_currency"],
        ]),
        "delivery_condition": get_first_available_value([rfq_data], [
            ["expected_delivery_conditions"],
            ["delivery_condition"],
            ["delivery_conditions"],
            ["incoterm"],
        ]),
        "payment_terms": get_first_available_value([rfq_data], [
            ["expected_payment_terms"],
            ["payment_terms"],
            ["payment_conditions"],
        ]),
    }
    commercial_data = {
        key: value
        for key, value in commercial_data.items()
        if field_has_value(value)
    }

    payload = {
        "project_number": project_number,
        "customer": get_first_available_value([rfq_data], [
            ["customer_name"],
            ["customer", "name"],
            ["customer"],
            ["account_name"],
        ]),
        "component_name": component_name,
        "part_numbers": part_numbers,
        "product_family": family_info["product_family"],
        "component_family": family_info["component_family"],
        "component_reference": component_reference,
        "annual_max_quantity": annual_max_quantity,
        "production_country": get_first_available_value([rfq_data], [
            ["country"],
            ["production_country"],
            ["country_to_produce"],
        ]),
        "destination_zone": get_first_available_value([rfq_data], [
            ["delivery_zone"],
            ["destination_zone"],
            ["zone"],
        ]),
        "technical_specification": technical_specification,
        "commercial_data": commercial_data,
        "documents": documents,
        "document_matches_component": document_matches_component,
        "document_component_mismatch": document_component_mismatch,
        "required_agent": None,
        "agent_to_use": None,
        "routing_reason": family_info["routing_reason"],
    }

    missing_for_costing_agent = []
    for field_name in [
        "project_number",
        "component_name",
        "component_family",
        "annual_max_quantity",
        "production_country",
        "destination_zone",
    ]:
        if not field_has_value(payload.get(field_name)):
            missing_for_costing_agent.append(field_name)

    technical_specification_available = field_has_value(technical_specification)
    usable_component_evidence = technical_specification_available or document_matches_component

    if not technical_specification_available:
        missing_for_costing_agent.append("Technical specification is missing")

    if not usable_component_evidence:
        missing_for_costing_agent.append("Correct component drawing/specification is required")

    if family_info.get("is_assembly_rfq") and not usable_component_evidence:
        missing_for_costing_agent.append(
            "Component-level routing must be confirmed for assembly RFQ"
        )

    if document_component_mismatch:
        missing_for_costing_agent.append("Correct component drawing/specification is required")

    missing_for_costing_agent = list(dict.fromkeys(missing_for_costing_agent))

    payload["missing_for_costing_agent"] = missing_for_costing_agent
    payload["payload_status"] = (
        "incomplete_for_costing_agent"
        if missing_for_costing_agent
        else "ready_for_external_component_costing_agent"
    )

    if is_complete_choke_context(rfq_data, family_info, component_name):
        payload["payload_status"] = "requires_choke_decomposition_workflow"
        payload["required_agent"] = "Costing Workflow Router"
        payload["agent_to_use"] = "Costing Workflow Router"
        payload["routing_reason"] = CHOKE_DECOMPOSITION_ROUTING_REASON
        payload["workflow_hint"] = {
            "workflow_type": "choke_decomposition_internal_costing",
            "required_steps": CHOKE_DECOMPOSITION_WORKFLOW_STEPS,
        }
    elif payload["payload_status"] == "ready_for_external_component_costing_agent":
        payload["required_agent"] = "External Component Costing Expert"
        payload["agent_to_use"] = "External Component Costing Expert"

    return payload


def build_costing_test_payload_from_rfq_data(rfq_data):
    return build_external_component_costing_payload_from_rfq_data(rfq_data)


def extract_costing_test_record_metadata(rfq_data, input_payload):
    source_data = rfq_data if isinstance(rfq_data, dict) else {}
    products = get_costing_product_records(source_data)
    components = get_costing_component_records(source_data)
    first_product = products[0] if products else {}
    first_component = components[0] if components else {}
    part_numbers = input_payload.get("part_numbers") or []

    product_reference = get_first_available_value([first_product, source_data], [
        ["part_number"],
        ["customer_part_number"],
        ["product_reference"],
        ["customer_reference"],
    ])
    component_reference = get_first_available_value([first_component, source_data], [
        ["component_part_number"],
        ["component_code_or_drawing_number"],
        ["component_code"],
        ["drawing_number"],
        ["component_reference"],
        ["part_number"],
    ])
    component_name = get_first_available_value([first_component, source_data, first_product], [
        ["component_name"],
        ["description"],
        ["product_name"],
        ["name"],
    ])

    return {
        "product_reference": product_reference or (part_numbers[0] if part_numbers else None),
        "component_reference": component_reference or (part_numbers[0] if part_numbers else None),
        "component_name": component_name or input_payload.get("component_name") or input_payload.get("component_family"),
    }


def save_component_costing_test(
    scenario_name,
    input_payload,
    expected_output=None,
    actual_output=None,
    test_status="not_run",
    source_data=None,
):
    metadata = extract_costing_test_record_metadata(source_data, input_payload)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO component_costing_tests
                (
                    scenario_name,
                    project_code,
                    customer,
                    product_reference,
                    component_reference,
                    component_name,
                    product_family,
                    costing_agent,
                    input_payload,
                    expected_output,
                    actual_output,
                    test_status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING component_costing_test_id
            """, (
                scenario_name,
                input_payload.get("project_number") or "UNKNOWN_PROJECT",
                input_payload.get("customer"),
                metadata["product_reference"],
                metadata["component_reference"],
                metadata["component_name"],
                input_payload.get("product_family"),
                input_payload.get("agent_to_use") or "External Component Costing Expert",
                Json(input_payload),
                expected_output,
                Json(actual_output) if actual_output is not None else None,
                test_status,
            ))
            test_id = cur.fetchone()["component_costing_test_id"]

        conn.commit()

    return test_id


def update_component_costing_test_result(
    component_costing_test_id,
    actual_output,
    test_status,
    expected_output=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE component_costing_tests
                SET
                    expected_output = %s,
                    actual_output = %s,
                    test_status = %s,
                    updated_at = NOW()
                WHERE component_costing_test_id = %s
            """, (
                expected_output,
                Json(actual_output) if actual_output is not None else None,
                test_status,
                component_costing_test_id,
            ))
        conn.commit()


def load_component_costing_tests():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    component_costing_test_id,
                    project_code,
                    component_name,
                    product_family,
                    costing_agent,
                    test_status,
                    created_at
                FROM component_costing_tests
                ORDER BY component_costing_test_id DESC
                LIMIT 100
            """)
            return cur.fetchall()


def workflow_text(payload):
    values = [
        payload.get("product_family"),
        payload.get("component_family"),
        payload.get("product_name"),
        payload.get("component_name"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def route_has_any_value(payload, candidate_keys):
    for key in candidate_keys:
        value = find_value_by_keys(payload, [key])
        if field_has_value(value):
            return True

    return False


def get_first_part_number(payload):
    part_numbers = payload.get("part_numbers")
    if isinstance(part_numbers, list) and part_numbers:
        return part_numbers[0]

    return None


def build_choke_missing_inputs(payload):
    checks = [
        (
            "ferrite grade",
            ["ferrite_grade", "core_grade", "ferrite_material_grade"],
        ),
        (
            "ferrite quantity/weight",
            [
                "ferrite_quantity",
                "ferrite_weight",
                "ferrite_weight_grams",
                "core_quantity",
                "core_weight",
                "core_weight_grams",
            ],
        ),
        (
            "wire quantity/weight",
            [
                "wire_quantity",
                "wire_weight",
                "wire_weight_grams",
                "wire_length",
                "wire_length_mm",
                "wire_length_m",
            ],
        ),
        (
            "tin quantity",
            ["tin_quantity", "tin_weight", "tin_weight_grams", "tinning_quantity"],
        ),
        (
            "plant manufacturing route",
            [
                "plant_manufacturing_route",
                "manufacturing_route",
                "routing_operations",
                "router_operations",
                "operation_sequence",
            ],
        ),
        (
            "operation cycle time/rate",
            [
                "operation_cycle_time",
                "cycle_time",
                "cycle_time_seconds",
                "rate",
                "pieces_per_hour",
                "strokes_per_hour",
            ],
        ),
        (
            "plant/unit cost data",
            [
                "plant_unit_cost_data",
                "plant_cost_data",
                "unit_cost_data",
                "cost_structure",
                "plant_hourly_rate",
            ],
        ),
    ]
    missing_inputs = []

    for label, keys in checks:
        if not route_has_any_value(payload, keys):
            missing_inputs.append(label)

    return missing_inputs


def build_costing_workflow_route_from_payload(payload):
    product_family = payload.get("product_family") or payload.get("component_family") or "Unknown"
    route_text = workflow_text(payload)
    workflow_status = "draft"
    missing_inputs = []

    project_code = normalize_project_code(
        payload.get("project_number")
        or payload.get("project_code")
        or payload.get("systematic_rfq_id")
        or payload.get("systematicRfqId")
    ) or "UNKNOWN_PROJECT"
    product_reference = (
        payload.get("product_reference")
        or payload.get("component_reference")
        or get_first_part_number(payload)
    )
    product_name = payload.get("product_name") or payload.get("component_name")

    if "choke" in route_text:
        workflow_type = "choke_decomposition_internal_costing"
        required_steps = [
            "define_ferrite_and_quantity",
            "define_wire_and_quantity",
            "define_tin_and_quantity",
            "cost_external_components",
            "calculate_added_value",
            "apply_plant_manufacturing_cost",
            "calculate_full_pnl",
            "run_npv_balancing_macro",
        ]
        route_reason = (
            "Choke must be decomposed into ferrite, wire, tin, added value, "
            "plant cost, P&L, and NPV balancing before costing."
        )
        missing_inputs = build_choke_missing_inputs(payload)
    elif any(
        keyword in route_text
        for keyword in [
            "spring",
            "plastic",
            "electronic",
            "pth",
            "ferrite external",
            "external-only",
            "external only",
            "enameled wire",
            "enamelled wire",
            "stamping",
            "stamped",
            "busbar",
        ]
    ):
        workflow_type = "external_component_costing"
        required_steps = [
            "prepare_external_component_payload",
            "call_external_component_costing_expert",
            "store_component_costing_json",
        ]
        route_reason = "Component is routed to the external component costing workflow."
    elif "assembly" in route_text:
        workflow_type = "assembly_bom_decomposition"
        required_steps = [
            "extract_bom",
            "classify_components",
            "route_each_component",
            "build_router_with_most",
            "calculate_full_pnl",
        ]
        route_reason = "Assembly must be decomposed into BOM and routed component by component."
    elif "brush" in route_text:
        workflow_type = "internal_brush_costing"
        required_steps = [
            "prepare_internal_brush_payload",
            "call_internal_brush_costing_when_available",
            "store_internal_costing_json",
        ]
        route_reason = "Brush should use the internal brush costing workflow."
    else:
        workflow_type = "manual_costing_triage"
        workflow_status = "blocked"
        required_steps = ["send_to_costing_expert"]
        route_reason = "Workflow type could not be determined automatically."
        missing_inputs = ["supported product/component family"]

    return {
        "project_code": project_code,
        "product_reference": product_reference,
        "product_name": product_name,
        "product_family": product_family,
        "component_family": payload.get("component_family"),
        "workflow_type": workflow_type,
        "workflow_status": workflow_status,
        "route_reason": route_reason,
        "required_steps": required_steps,
        "missing_inputs": missing_inputs,
    }


def save_costing_workflow_route(route):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO costing_workflow_routes
                (
                    project_code,
                    product_reference,
                    product_name,
                    product_family,
                    workflow_type,
                    workflow_status,
                    route_reason,
                    required_steps,
                    missing_inputs
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING costing_workflow_route_id
            """, (
                route.get("project_code") or "UNKNOWN_PROJECT",
                route.get("product_reference"),
                route.get("product_name"),
                route.get("product_family") or "Unknown",
                route.get("workflow_type"),
                route.get("workflow_status") or "draft",
                route.get("route_reason"),
                Json(route.get("required_steps") or []),
                Json(route.get("missing_inputs") or []),
            ))
            route_id = cur.fetchone()["costing_workflow_route_id"]

        conn.commit()

    return route_id


def load_costing_workflow_routes():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    costing_workflow_route_id,
                    project_code,
                    product_reference,
                    product_name,
                    product_family,
                    workflow_type,
                    workflow_status,
                    route_reason,
                    created_at
                FROM costing_workflow_routes
                ORDER BY costing_workflow_route_id DESC
                LIMIT 100
            """)
            return cur.fetchall()


def numeric_value(value):
    if value in [None, ""]:
        return None

    coerced = coerce_number(value)
    if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
        return float(coerced)

    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def interpolate_winding_time_per_turn(wire_diameter_mm):
    wire_diameter = numeric_value(wire_diameter_mm)
    if wire_diameter is None:
        return None

    min_diameter = 0.3
    max_diameter = 2.5
    min_time = 0.19
    max_time = 0.37

    if wire_diameter <= min_diameter:
        return min_time
    if wire_diameter >= max_diameter:
        return max_time

    ratio = (wire_diameter - min_diameter) / (max_diameter - min_diameter)
    return min_time + ratio * (max_time - min_time)


def calculate_winding_operation(wire_diameter_mm, number_of_turns, tin_thickness_micron):
    wire_diameter = numeric_value(wire_diameter_mm)
    turns = numeric_value(number_of_turns)
    tin_thickness = numeric_value(tin_thickness_micron)

    time_per_turn = interpolate_winding_time_per_turn(wire_diameter)
    fixed_time_seconds = 1.5 if tin_thickness is not None and tin_thickness < 10 else 0.5
    cycle_time_seconds = None
    if time_per_turn is not None and turns is not None:
        cycle_time_seconds = fixed_time_seconds + turns * time_per_turn

    base_specific_capex = 18000 if wire_diameter is not None and wire_diameter >= 1.6 else 14000
    additional_specific_capex = 1000
    capex_detail = {
        "base_winding_machine_eur": base_specific_capex,
        "fixturing_eur": 1000,
    }
    if tin_thickness is not None and tin_thickness < 10:
        additional_specific_capex += 4000
        capex_detail["thin_tin_additional_capex_eur"] = 4000

    return {
        "operation_name": "winding",
        "operation_type": "stand alone",
        "input_parameters": {
            "wire_diameter_mm": wire_diameter,
            "number_of_turns": turns,
            "tin_thickness_micron": tin_thickness,
        },
        "fixed_time_seconds": fixed_time_seconds,
        "time_per_turn_seconds": time_per_turn,
        "cycle_time_seconds": cycle_time_seconds,
        "oee_percent": 75,
        "operator_percent": 25,
        "parts_per_cycle": 1,
        "specific_capex_eur": base_specific_capex + additional_specific_capex,
        "specific_capex_detail": capex_detail,
        "tooling": {
            "initial_tooling_cost_eur": 2500,
            "warranty_price_eur": 500,
            "lifetime_parts": 250000,
            "tooling_adder_per_piece_eur": 0.002,
        },
        "most_payload": {
            "source": "back_office_parameters",
            "sharepoint_access_allowed": False,
            "operation": "winding",
            "cycle_time_seconds": cycle_time_seconds,
            "operator_percent": 25,
            "parts_per_cycle": 1,
        },
    }


def determine_core_insertion_type(drawing_analysis):
    analysis = drawing_analysis or {}
    reasons = []
    glued_signals = []
    locked_signals = []

    if analysis.get("explicit_glued"):
        glued_signals.append("explicit glued instruction")
    if analysis.get("glue_mentioned"):
        glued_signals.append("glue is mentioned")
    if analysis.get("ferrite_push_out_force_mentioned"):
        glued_signals.append("ferrite push out force is mentioned")
    if analysis.get("one_or_both_flat_faces_without_wire"):
        glued_signals.append("one or both flat faces are without wire")

    if analysis.get("explicit_locked"):
        locked_signals.append("explicit locked instruction")
    if (
        not analysis.get("glue_mentioned")
        and not analysis.get("ferrite_push_out_force_mentioned")
        and analysis.get("wire_crosses_both_flat_faces")
    ):
        locked_signals.append(
            "no glue, no ferrite push out force, and wire crosses both flat faces"
        )

    if glued_signals:
        reasons.extend(glued_signals)
        if locked_signals:
            reasons.append("glued chosen because conflicting/doubtful evidence defaults to glued")
        return {"core_option": "glued", "reason": reasons}

    if locked_signals:
        reasons.extend(locked_signals)
        return {"core_option": "locked", "reason": reasons}

    return {
        "core_option": "glued",
        "reason": ["doubtful or incomplete drawing evidence defaults to glued"],
    }


def calculate_core_insertion_operation(core_option, annual_quantity):
    annual_qty = numeric_value(annual_quantity) or 0
    is_high_volume = annual_qty >= 800000

    if core_option == "locked" and not is_high_volume:
        return {
            "operation_name": "core_insertion",
            "core_option": "locked",
            "mode": "manual",
            "operation_type": "stand alone",
            "operator_percent": 100,
            "machine_rate_parts_per_hour": 800,
            "parts_per_cycle": 1,
            "generic_capex_eur": 2000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_life_parts": 800000,
            "warranty_tooling_cost_eur": 500,
        }

    if core_option == "locked":
        return {
            "operation_name": "core_insertion",
            "core_option": "locked",
            "mode": "automatic",
            "operation_type": "stand alone",
            "operator_percent": 25,
            "machine_rate_parts_per_hour": 1600,
            "parts_per_cycle": 1,
            "generic_capex_eur": 10000,
            "specific_capex_eur": 5000,
            "tooling_cost_eur": 1000,
            "tooling_life_parts": 800000,
            "warranty_tooling_cost_eur": 500,
        }

    if not is_high_volume:
        return {
            "operation_name": "core_insertion",
            "core_option": "glued",
            "mode": "manual",
            "operation_type": "stand alone",
            "operator_percent": 100,
            "machine_rate_parts_per_hour": 800,
            "parts_per_cycle": 1,
            "generic_capex_eur": 11000,
            "generic_capex_detail": {
                "gluing_station_eur": 2000,
                "oven_eur": 9000,
            },
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_life_parts": 800000,
            "warranty_tooling_cost_eur": 500,
        }

    return {
        "operation_name": "core_insertion",
        "core_option": "glued",
        "mode": "automatic",
        "operation_type": "stand alone",
        "operator_percent": 25,
        "machine_rate_parts_per_hour": 1300,
        "parts_per_cycle": 1,
        "generic_capex_eur": 19000,
        "generic_capex_detail": {
            "gluing_station_eur": 10000,
            "oven_eur": 9000,
        },
        "specific_capex_eur": 1000,
        "tooling_cost_eur": 1000,
        "tooling_life_parts": 800000,
        "warranty_tooling_cost_eur": 500,
    }


def calculate_extra_bending_operations(left_direction_changes, right_direction_changes):
    left_changes = numeric_value(left_direction_changes) or 0
    right_changes = numeric_value(right_direction_changes) or 0

    if left_changes <= 2 and right_changes <= 2:
        bend_count = 0
    elif left_changes > 2 and right_changes > 2:
        bend_count = 2
    else:
        bend_count = 1

    operations = []
    for index in range(int(bend_count)):
        operations.append({
            "operation_name": f"extra_manual_bending_{index + 1}",
            "mode": "manual",
            "operation_type": "stand alone",
            "operator_percent": 100,
            "machine_rate_parts_per_hour": 800,
            "parts_per_cycle": 1,
            "generic_capex_eur": 2000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_life_parts": 800000,
            "warranty_tooling_cost_eur": 500,
        })

    return operations


def calculate_push_test_operation(core_option, annual_quantity):
    if core_option == "locked":
        return None

    annual_qty = numeric_value(annual_quantity) or 0
    if annual_qty < 800000:
        return {
            "operation_name": "push_test",
            "core_option": "glued",
            "mode": "manual",
            "operation_type": "stand alone",
            "operator_percent": 100,
            "machine_rate_parts_per_hour": 1200,
            "parts_per_cycle": 1,
            "generic_capex_eur": 3000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_life_parts": 800000,
            "warranty_tooling_cost_eur": 500,
        }

    return {
        "operation_name": "push_test",
        "core_option": "glued",
        "mode": "automatic",
        "operation_type": "stand alone",
        "operator_percent": 20,
        "machine_rate_parts_per_hour": 1400,
        "parts_per_cycle": 1,
        "generic_capex_eur": 4000,
        "specific_capex_eur": 2000,
        "tooling_cost_eur": 1000,
        "tooling_life_parts": 800000,
        "warranty_tooling_cost_eur": 500,
    }


def calculate_final_inspection_packaging(core_option, annual_quantity):
    annual_qty = numeric_value(annual_quantity) or 0
    if core_option == "glued" and annual_qty < 800000:
        return {
            "operation_name": "final_inspection_packaging",
            "core_option": "glued",
            "mode": "manual",
            "operation_type": "stand alone",
            "operator_percent": 100,
            "machine_rate_parts_per_hour": 2500,
            "parts_per_cycle": 1,
            "generic_capex_eur": 4000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 0,
        }

    return {
        "missing_rule": (
            "final_inspection_packaging rule is only defined for glued "
            "core_option with annual_quantity < 800000"
        )
    }


def calculate_glue_material(ferrite_diameter_mm, ferrite_length_mm):
    ferrite_diameter = numeric_value(ferrite_diameter_mm)
    ferrite_length = numeric_value(ferrite_length_mm)

    if ferrite_diameter is None or ferrite_length is None:
        return {
            "required": None,
            "missing_inputs": ["ferrite_diameter_mm", "ferrite_length_mm"],
        }

    if ferrite_diameter <= 6:
        return {
            "required": False,
            "reason": "Ferrite diameter <= 6 mm: no glue by rule",
            "cost_per_piece_eur": 0,
            "mass_grams": 0,
        }

    strip_count = 2
    strip_diameter_mm = 1
    strip_length_mm = 0.8 * ferrite_length
    radius_cm = (strip_diameter_mm / 10) / 2
    strip_length_cm = strip_length_mm / 10
    volume_per_strip_cm3 = math.pi * (radius_cm ** 2) * strip_length_cm
    total_volume_cm3 = volume_per_strip_cm3 * strip_count
    density_g_per_cm3 = 2
    mass_grams = total_volume_cm3 * density_g_per_cm3
    cost_per_piece_eur = (mass_grams / 1000) * 100

    return {
        "required": True,
        "strip_count": strip_count,
        "strip_diameter_mm": strip_diameter_mm,
        "strip_length_mm": strip_length_mm,
        "density_g_per_cm3": density_g_per_cm3,
        "cost_eur_per_kg": 100,
        "volume_per_strip_cm3": volume_per_strip_cm3,
        "total_volume_cm3": total_volume_cm3,
        "mass_grams": mass_grams,
        "cost_per_piece_eur": cost_per_piece_eur,
    }


def build_choke_workflow_v1(input_data):
    data = input_data or {}
    missing_inputs = []
    missing_rules = []
    most_operations = []

    for field_name in [
        "project_code",
        "product_name",
        "delivery_zone",
        "annual_quantity",
        "wire_diameter_mm",
        "number_of_turns",
        "tin_thickness_micron",
        "ferrite_diameter_mm",
        "ferrite_length_mm",
    ]:
        if not field_has_value(data.get(field_name)):
            missing_inputs.append(field_name)

    manufacturing_strategy = {
        "project_code": data.get("project_code"),
        "delivery_zone": data.get("delivery_zone"),
        "production_plant": data.get("production_plant"),
        "source": data.get("manufacturing_strategy_source") or "manual_or_not_provided",
    }
    if not field_has_value(manufacturing_strategy.get("production_plant")):
        missing_inputs.append("production plant from manufacturing strategy")

    drawing_analysis = data.get("drawing_analysis") or {}
    core_option = determine_core_insertion_type(drawing_analysis)
    glue_material = calculate_glue_material(
        data.get("ferrite_diameter_mm"),
        data.get("ferrite_length_mm"),
    )
    if glue_material.get("missing_inputs"):
        missing_inputs.extend(glue_material["missing_inputs"])

    material_decomposition = {
        "ferrite": {
            "diameter_mm": numeric_value(data.get("ferrite_diameter_mm")),
            "length_mm": numeric_value(data.get("ferrite_length_mm")),
            "grade": data.get("ferrite_grade"),
            "quantity_per_piece": data.get("ferrite_quantity_per_piece"),
        },
        "wire": {
            "diameter_mm": numeric_value(data.get("wire_diameter_mm")),
            "turns": numeric_value(data.get("number_of_turns")),
            "quantity_or_weight": data.get("wire_quantity_or_weight"),
        },
        "tin": {
            "thickness_micron": numeric_value(data.get("tin_thickness_micron")),
            "quantity": data.get("tin_quantity"),
        },
        "glue": glue_material,
    }

    winding_operation = calculate_winding_operation(
        data.get("wire_diameter_mm"),
        data.get("number_of_turns"),
        data.get("tin_thickness_micron"),
    )
    most_operations.append(winding_operation)
    most_operations.append(calculate_core_insertion_operation(
        core_option["core_option"],
        data.get("annual_quantity"),
    ))
    most_operations.extend(calculate_extra_bending_operations(
        data.get("left_direction_changes"),
        data.get("right_direction_changes"),
    ))

    push_test_operation = calculate_push_test_operation(
        core_option["core_option"],
        data.get("annual_quantity"),
    )
    if push_test_operation:
        most_operations.append(push_test_operation)

    final_inspection = calculate_final_inspection_packaging(
        core_option["core_option"],
        data.get("annual_quantity"),
    )
    if final_inspection.get("missing_rule"):
        missing_rules.append(final_inspection["missing_rule"])
    else:
        most_operations.append(final_inspection)

    for operation in most_operations:
        operation.setdefault("most_payload", {
            "source": "back_office_parameters",
            "sharepoint_access_allowed": False,
            "operation": operation.get("operation_name"),
            "mode": operation.get("mode"),
            "machine_rate_parts_per_hour": operation.get("machine_rate_parts_per_hour"),
            "operator_percent": operation.get("operator_percent"),
            "parts_per_cycle": operation.get("parts_per_cycle"),
        })

    next_steps = [
        "validate material decomposition",
        "prepare MOST operation payloads from back-office parameters",
        "send external sub-components only to the correct costing agents",
        "calculate added value and plant manufacturing cost",
        "calculate full P&L",
        "run NPV balancing macro",
    ]

    return {
        "workflow_type": "choke_decomposition_internal_costing",
        "manufacturing_strategy": manufacturing_strategy,
        "material_decomposition": material_decomposition,
        "core_option": core_option,
        "most_operations": most_operations,
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
        "missing_rules": list(dict.fromkeys(missing_rules)),
        "next_steps": next_steps,
    }


def parse_json_input(value, default=None):
    if default is None:
        default = {}

    if value in [None, ""]:
        return default

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        return json.loads(text)

    return value


def classify_choke_material(value):
    text = json.dumps(value, ensure_ascii=False, default=str).lower()
    text = text.replace("\u00e9", "e").replace("\u00e8", "e").replace("\u00ea", "e")

    if any(keyword in text for keyword in ["ferrite", "core"]):
        return "ferrite"
    if any(keyword in text for keyword in ["enameled wire", "enamelled wire", "copper wire", "wire", "fil cuivre", "fil"]):
        return "wire"
    if any(keyword in text for keyword in ["tinning", "tin", "etain"]):
        return "tin"
    if any(keyword in text for keyword in ["glue", "colle", "adhesive"]):
        return "glue"

    return None


def extract_material_quantity(value):
    if not isinstance(value, dict):
        return None

    return get_first_available_value([value], [
        ["quantity"],
        ["quantite"],
        ["qty"],
        ["quantity_per_piece"],
        ["quantity_per_unit"],
    ])


def extract_weight_kg(value):
    if not isinstance(value, dict):
        return None

    kg_value = get_first_available_value([value], [
        ["weight_kg"],
        ["mass_kg"],
        ["poids_kg"],
        ["weight_per_piece_kg"],
    ])
    if field_has_value(kg_value):
        return numeric_value(kg_value)

    gram_value = get_first_available_value([value], [
        ["weight_g"],
        ["weight_grams"],
        ["mass_g"],
        ["mass_grams"],
        ["poids_g"],
        ["mass_grams_per_piece"],
        ["weight_grams_per_piece"],
    ])
    if field_has_value(gram_value):
        grams = numeric_value(gram_value)
        return grams / 1000 if grams is not None else None

    text = json.dumps(value, ensure_ascii=False, default=str).lower()
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(kg|g)\b", text)
    if not match:
        return None

    number = numeric_value(match.group(1))
    if number is None:
        return None

    return number if match.group(2) == "kg" else number / 1000


def extract_cost_per_piece(value):
    if not isinstance(value, dict):
        return None

    cost = get_first_available_value([value], [
        ["cost_per_piece"],
        ["cost_per_piece_eur"],
        ["price_per_piece"],
        ["price_per_unit"],
        ["unit_price"],
        ["unit_cost"],
        ["selling_price_per_unit"],
        ["total_cost_per_piece"],
        ["calculated_cost_per_piece"],
    ])
    return numeric_value(cost) if field_has_value(cost) else None


def extract_price_per_kg(value):
    if not isinstance(value, dict):
        return None

    price = get_first_available_value([value], [
        ["material_price_eur_per_kg"],
        ["cost_eur_per_kg"],
        ["price_eur_per_kg"],
        ["unit_cost_eur_per_kg"],
        ["tin_material_price_eur_per_kg"],
    ])
    return numeric_value(price) if field_has_value(price) else None


def iter_json_dicts(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_json_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_dicts(item)


def normalize_material_line(material_type, raw_line):
    description = None
    if isinstance(raw_line, dict):
        description = get_first_available_value([raw_line], [
            ["product"],
            ["designation"],
            ["description"],
            ["name"],
            ["material"],
            ["produit"],
        ])

    return {
        "material_type": material_type,
        "description": description,
        "quantity": extract_material_quantity(raw_line) if isinstance(raw_line, dict) else None,
        "weight_kg": extract_weight_kg(raw_line) if isinstance(raw_line, dict) else None,
        "raw_line": raw_line,
    }


def is_probable_material_line(row):
    if not isinstance(row, dict):
        return False

    material_line_keys = [
        "poste",
        "material_type",
        "material",
        "produit",
        "produitdesignation",
        "product",
        "designation",
        "description",
        "specification",
        "quantity",
        "quantite",
        "qty",
        "weight_kg",
        "weight_g",
        "mass_kg",
        "mass_g",
        "poids_kg",
        "poids_g",
        "cost_per_piece",
        "cost_per_piece_eur",
    ]

    return any(get_dict_value(row, key) is not None for key in material_line_keys)


def parse_choke_bom_json(bom_json):
    data = parse_json_input(bom_json, default={})
    material_lines = []
    seen = set()

    material_decomposition = (
        data.get("material_decomposition")
        if isinstance(data, dict)
        else None
    )
    if isinstance(material_decomposition, dict):
        for material_type in ["ferrite", "wire", "tin", "glue"]:
            raw_line = material_decomposition.get(material_type)
            if field_has_value(raw_line):
                material_lines.append(normalize_material_line(material_type, raw_line))
                seen.add(json.dumps(raw_line, sort_keys=True, default=str))

    for row in iter_json_dicts(data):
        if row is data or row is material_decomposition:
            continue
        if not is_probable_material_line(row):
            continue

        material_type = classify_choke_material(row)
        if not material_type:
            continue

        row_key = json.dumps(row, sort_keys=True, default=str)
        if row_key in seen:
            continue

        material_lines.append(normalize_material_line(material_type, row))
        seen.add(row_key)

    return {
        "material_lines": material_lines,
        "raw_bom": data,
    }


def parse_external_costing_records(external_costing_jsons):
    data = parse_json_input(external_costing_jsons, default=[])

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        for key in ["component_costings", "costing_results", "results", "items", "offers"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]

    return []


def find_external_cost_for_material(material_type, external_records):
    for record in external_records:
        record_type = classify_choke_material(record)
        if record_type != material_type:
            continue

        cost = extract_cost_per_piece(record)
        if cost is not None:
            return cost, record

    return None, None


def calculate_material_cost_from_bom(bom_json, external_costing_jsons):
    parsed_bom = parse_choke_bom_json(bom_json)
    external_records = parse_external_costing_records(external_costing_jsons)
    material_lines = []
    missing_material_costs = []
    total_cost = 0.0

    lines_by_type = {}
    for line in parsed_bom["material_lines"]:
        lines_by_type.setdefault(line["material_type"], []).append(line)

    for required_material in ["ferrite", "wire", "tin"]:
        if required_material not in lines_by_type:
            missing_material_costs.append(f"{required_material} BOM line missing")

    for line in parsed_bom["material_lines"]:
        material_type = line["material_type"]
        cost_per_piece = None
        costing_source = None

        if material_type in ["ferrite", "wire"]:
            cost_per_piece, external_record = find_external_cost_for_material(
                material_type,
                external_records,
            )
            if cost_per_piece is not None:
                costing_source = "external_component_costing_expert"
                line["external_costing_record"] = external_record

        if cost_per_piece is None:
            cost_per_piece = extract_cost_per_piece(line.get("raw_line"))
            if cost_per_piece is not None:
                costing_source = "bom_cost_per_piece"

        if cost_per_piece is None and material_type == "tin":
            weight_kg = line.get("weight_kg")
            price_per_kg = extract_price_per_kg(line.get("raw_line"))
            if weight_kg is not None and price_per_kg is not None:
                cost_per_piece = weight_kg * price_per_kg
                costing_source = "tin_material_price"

        if cost_per_piece is None and material_type == "glue":
            weight_kg = line.get("weight_kg")
            if weight_kg is not None:
                cost_per_piece = weight_kg * 100
                costing_source = "glue_rule_100_eur_per_kg"

        if cost_per_piece is None:
            missing_material_costs.append(f"{material_type} cost missing")
        else:
            total_cost += cost_per_piece

        material_lines.append({
            **line,
            "cost_per_piece": cost_per_piece,
            "costing_source": costing_source,
        })

    return {
        "material_cost_per_piece": total_cost,
        "material_lines": material_lines,
        "missing_material_costs": list(dict.fromkeys(missing_material_costs)),
    }


def parse_most_operations(operations_json):
    data = parse_json_input(operations_json, default=[])

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        for key in ["most_operations", "operations", "operation_costs", "routing_operations"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]

    return []


def operation_value(operation, candidate_keys):
    for key in candidate_keys:
        value = find_value_by_keys(operation, [key])
        if field_has_value(value):
            return value
    return None


def calculate_operation_produced_per_hour(operation):
    p_h = operation_value(operation, [
        "p_h",
        "produced_per_hour",
        "machine_rate_parts_per_hour",
        "machine_rate",
        "rate_parts_per_hour",
        "pieces_per_hour",
    ])
    p_h = numeric_value(p_h)
    if p_h is not None and p_h > 0:
        return p_h

    cycle_time = numeric_value(operation_value(operation, [
        "cycle_time_seconds",
        "cycle_time_sec",
        "cycle_time",
    ]))
    parts_per_cycle = numeric_value(operation_value(operation, [
        "parts_per_cycle",
        "pieces_per_cycle",
        "parts_per_stroke",
    ])) or 1

    if cycle_time is None or cycle_time <= 0:
        return None

    return 3600 / cycle_time * parts_per_cycle


def normalize_oee(value):
    if value in [None, ""]:
        return 1.0

    oee = numeric_value(value)
    if oee is None:
        return 1.0
    if oee > 1:
        return oee / 100
    return oee


def calculate_added_value_from_most(operations_json, plant_data, commercial_data):
    operations = parse_most_operations(operations_json)
    direct_labor_rate = numeric_value(
        plant_data.get("direct_labor_rate_operating_currency_per_hour")
    )
    voh_rate = numeric_value(plant_data.get("voh_rate_operating_currency_per_hour"))
    plant_open_hours = numeric_value(plant_data.get("plant_open_hours_per_year"))
    fx_rate = numeric_value(plant_data.get("fx_rate_operating_to_selling"))
    annual_quantity = numeric_value(commercial_data.get("annual_quantity"))

    missing_inputs = []
    for field_name, value in [
        ("direct_labor_rate_operating_currency_per_hour", direct_labor_rate),
        ("voh_rate_operating_currency_per_hour", voh_rate),
        ("plant_open_hours_per_year", plant_open_hours),
        ("fx_rate_operating_to_selling", fx_rate),
        ("annual_quantity", annual_quantity),
    ]:
        if value in [None, 0]:
            missing_inputs.append(field_name)

    operation_costs = []
    total_direct_labor = 0.0
    total_voh = 0.0

    for index, operation in enumerate(operations, start=1):
        operation_name = operation.get("operation_name") or operation.get("operation") or f"operation_{index}"
        p_h = calculate_operation_produced_per_hour(operation)
        oee = normalize_oee(operation_value(operation, ["oee", "oee_percent", "costing_oee_percent"]))
        operator_percent = numeric_value(operation_value(operation, ["operator_percent", "percent_operator"]))
        generic_capex = numeric_value(operation_value(operation, ["generic_capex_eur", "generic_capex"])) or 0
        specific_capex = numeric_value(operation_value(operation, ["specific_capex_eur", "specific_capex"])) or 0
        tooling_adder = numeric_value(operation_value(operation, ["tooling_adder_per_piece_eur"]))
        tooling_cost = numeric_value(operation_value(operation, ["tooling_cost_eur", "tooling_cost"]))
        tooling_life = numeric_value(operation_value(operation, ["tooling_life_pieces", "tooling_life_parts", "tooling_lifetime_parts"]))

        operation_missing = []
        if p_h in [None, 0]:
            operation_missing.append("p_h or cycle_time_seconds")
        if operator_percent is None:
            operation_missing.append("operator_percent")

        if missing_inputs or operation_missing:
            operation_costs.append({
                "operation_name": operation_name,
                "missing_inputs": missing_inputs + operation_missing,
                "raw_operation": operation,
            })
            continue

        produced_per_hour_after_oee = p_h * oee
        if produced_per_hour_after_oee <= 0:
            operation_costs.append({
                "operation_name": operation_name,
                "missing_inputs": ["produced_per_hour_after_oee"],
                "raw_operation": operation,
            })
            continue

        hm_mach_per_1000 = 1000 / produced_per_hour_after_oee
        hm_dl_per_1000 = hm_mach_per_1000 * operator_percent / 100
        hourly_cost_selling_currency = direct_labor_rate / fx_rate
        dl_cost_per_piece = hm_dl_per_1000 * hourly_cost_selling_currency / 1000

        yearly_production_hours = annual_quantity / produced_per_hour_after_oee
        base_voh_rate_selling_currency = voh_rate / fx_rate
        occupation_rate = yearly_production_hours / plant_open_hours * 1.1
        generic_capex_allocated = generic_capex * occupation_rate
        generic_maintenance_energy_cost = 0.15 * generic_capex_allocated
        generic_voh_per_hour = (
            generic_maintenance_energy_cost / yearly_production_hours
            if yearly_production_hours > 0
            else 0
        )
        specific_occupation_integer = int(occupation_rate) + 1
        specific_capex_allocated = specific_capex * specific_occupation_integer
        specific_maintenance_energy_cost = 0.15 * specific_capex_allocated
        specific_voh_per_hour = (
            specific_maintenance_energy_cost / yearly_production_hours
            if yearly_production_hours > 0
            else 0
        )

        if tooling_adder is not None:
            tooling_voh_per_hour = tooling_adder * produced_per_hour_after_oee
        elif tooling_cost is not None and tooling_life not in [None, 0]:
            tooling_voh_per_hour = tooling_cost / tooling_life * produced_per_hour_after_oee
        else:
            tooling_voh_per_hour = 0

        total_voh_per_hour = (
            base_voh_rate_selling_currency
            + generic_voh_per_hour
            + specific_voh_per_hour
            + tooling_voh_per_hour
        )
        voh_cost_per_piece = hm_mach_per_1000 * total_voh_per_hour / 1000

        total_direct_labor += dl_cost_per_piece
        total_voh += voh_cost_per_piece
        operation_costs.append({
            "operation_name": operation_name,
            "p_h": p_h,
            "oee": oee,
            "produced_per_hour_after_oee": produced_per_hour_after_oee,
            "hm_mach_per_1000": hm_mach_per_1000,
            "hm_dl_per_1000": hm_dl_per_1000,
            "dl_cost_per_piece": dl_cost_per_piece,
            "yearly_production_hours": yearly_production_hours,
            "occupation_rate": occupation_rate,
            "generic_voh_per_hour": generic_voh_per_hour,
            "specific_voh_per_hour": specific_voh_per_hour,
            "tooling_voh_per_hour": tooling_voh_per_hour,
            "total_voh_per_hour": total_voh_per_hour,
            "voh_cost_per_piece": voh_cost_per_piece,
            "raw_operation": operation,
        })

    return {
        "direct_labor_cost_per_piece": total_direct_labor,
        "voh_cost_per_piece": total_voh,
        "added_value_cost_per_piece": total_direct_labor + total_voh,
        "operation_costs": operation_costs,
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
    }


def calculate_total_choke_cost(material_result, added_value_result):
    missing_inputs = []
    not_commercially_usable_until = [
        "commercial scenario, margin, indexation, cashflow and approval steps are completed"
    ]

    missing_material_costs = material_result.get("missing_material_costs") or []
    if missing_material_costs:
        missing_inputs.extend(missing_material_costs)
        not_commercially_usable_until.append("all material costs are available")

    added_missing = added_value_result.get("missing_inputs") or []
    if added_missing:
        missing_inputs.extend(added_missing)
        not_commercially_usable_until.append("all MOST operation and plant inputs are available")

    for operation_cost in added_value_result.get("operation_costs") or []:
        for missing in operation_cost.get("missing_inputs") or []:
            missing_inputs.append(f"{operation_cost.get('operation_name')}: {missing}")

    material_cost = material_result.get("material_cost_per_piece") or 0
    direct_labor_cost = added_value_result.get("direct_labor_cost_per_piece") or 0
    voh_cost = added_value_result.get("voh_cost_per_piece") or 0
    added_value_cost = added_value_result.get("added_value_cost_per_piece") or 0

    return {
        "material_cost_per_piece": material_cost,
        "direct_labor_cost_per_piece": direct_labor_cost,
        "voh_cost_per_piece": voh_cost,
        "added_value_cost_per_piece": added_value_cost,
        "total_preliminary_choke_cost_per_piece": material_cost + added_value_cost,
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
        "not_commercially_usable_until": list(dict.fromkeys(not_commercially_usable_until)),
    }


def build_choke_customer_input_from_rfq(rfq_data):
    data = parse_json_input(rfq_data, default={})
    products = extract_rfq_products(data)
    first_product = products[0] if products else {}
    project_code = normalize_project_code(get_first_available_value([data], [
        ["systematic_rfq_id"],
        ["systematicRfqId"],
        ["project_number"],
        ["project_code"],
        ["rfq_id"],
    ]))
    product_name, _source = resolve_product_name(first_product, data)

    return {
        "project_code": project_code,
        "customer": get_first_available_value([data], [
            ["customer_name"],
            ["customer", "name"],
            ["customer"],
            ["account_name"],
        ]),
        "final_customer": get_first_available_value([data], [
            ["final_customer"],
            ["end_customer"],
            ["final_customer_name"],
        ]),
        "product_name": product_name,
        "part_number": get_first_available_value([first_product, data], [
            ["part_number"],
            ["customer_part_number"],
            ["product_reference"],
            ["customer_reference"],
        ]),
        "delivery_area": get_first_available_value([first_product, data], [
            ["delivery_area"],
            ["delivery_zone"],
            ["destination_zone"],
            ["zone"],
        ]),
        "currency": get_first_available_value([first_product, data], [
            ["target_price_currency"],
            ["currency"],
            ["price_currency"],
        ]),
        "target_price": coerce_number(get_first_available_value([first_product, data], [
            ["target_price"],
            ["target_price_eur"],
            ["target_price_local"],
            ["target_price_value"],
        ])),
        "annual_quantity": coerce_number(get_first_available_value([first_product, data], [
            ["annual_quantity"],
            ["annual_volume"],
            ["quantity"],
            ["qmax"],
            ["annual_max_quantity"],
        ])),
        "sop_year": get_first_available_value([first_product, data], [
            ["sop_year"],
            ["sop"],
            ["sop_date"],
        ]),
        "drawing_files": extract_rfq_files(data),
    }


def extract_line_technical_specification(raw_line):
    if not isinstance(raw_line, dict):
        return {}

    explicit_spec = get_first_available_value([raw_line], [
        ["technical_specification"],
        ["specification"],
        ["spec"],
        ["technical_data"],
        ["details"],
    ])
    if isinstance(explicit_spec, dict):
        return explicit_spec
    if field_has_value(explicit_spec):
        return {"text": explicit_spec}

    spec = {}
    for key in [
        "diameter_mm",
        "length_mm",
        "grade",
        "material",
        "inductance_uH",
        "turns",
        "tin_thickness_micron",
        "density_g_per_cm3",
        "cost_eur_per_kg",
    ]:
        value = get_dict_value(raw_line, key)
        if field_has_value(value):
            spec[key] = value

    return spec


def extract_weight_g(raw_line):
    weight_kg = extract_weight_kg(raw_line)
    return weight_kg * 1000 if weight_kg is not None else None


def normalize_choke_bom(bom_json):
    parsed_bom = parse_choke_bom_json(bom_json)
    lines_by_type = {}
    for line in parsed_bom["material_lines"]:
        lines_by_type.setdefault(line["material_type"], []).append(line)

    normalized_lines = []
    for component_type in ["ferrite", "wire", "tin", "glue"]:
        source_lines = lines_by_type.get(component_type) or [None]
        for line in source_lines:
            raw_line = line.get("raw_line") if isinstance(line, dict) else {}
            missing_data = []
            if not line:
                missing_data.append("BOM line missing")
            if not field_has_value(extract_material_quantity(raw_line)):
                missing_data.append("quantity_per_choke missing")
            if extract_weight_g(raw_line) is None and component_type in ["tin", "glue"]:
                missing_data.append("weight_per_piece_g missing")

            if component_type in ["ferrite", "wire"]:
                costing_route = "external_component_costing_expert"
            elif component_type == "tin":
                costing_route = "material_price_lookup"
            else:
                costing_route = "rule_based_cost" if extract_weight_g(raw_line) is not None else "rule_based_cost_missing_weight"

            confidence = None
            if isinstance(raw_line, dict):
                confidence = get_first_available_value([raw_line], [
                    ["confidence"],
                    ["confidence_level"],
                    ["extraction_confidence"],
                ])
            if confidence is None:
                confidence = 0 if not line else 0.8

            normalized_lines.append({
                "component_type": component_type,
                "quantity_per_choke": extract_material_quantity(raw_line),
                "technical_specification": extract_line_technical_specification(raw_line),
                "weight_per_piece_g": extract_weight_g(raw_line),
                "costing_route": costing_route,
                "confidence": confidence,
                "missing_data": missing_data,
                "source_line": raw_line,
            })

    return {
        "component_lines": normalized_lines,
        "missing_data": list(dict.fromkeys(
            f"{line['component_type']}: {missing}"
            for line in normalized_lines
            for missing in line["missing_data"]
        )),
    }


def build_component_cost_requests(normalized_bom):
    lines = normalized_bom.get("component_lines", []) if isinstance(normalized_bom, dict) else []
    requests = []

    for line in lines:
        component_type = line.get("component_type")
        if component_type in ["ferrite", "wire"]:
            requests.append({
                "component_type": component_type,
                "agent_to_use": "External Component Costing Expert",
                "payload": {
                    "component_family": component_type,
                    "quantity_per_choke": line.get("quantity_per_choke"),
                    "technical_specification": line.get("technical_specification"),
                    "weight_per_piece_g": line.get("weight_per_piece_g"),
                    "source": "choke_bom_analyzer",
                },
                "missing_data": line.get("missing_data") or [],
            })
        elif component_type == "tin":
            requests.append({
                "component_type": "tin",
                "agent_to_use": None,
                "payload_type": "material_price_lookup",
                "payload": {
                    "material": "tin",
                    "weight_per_piece_g": line.get("weight_per_piece_g"),
                    "technical_specification": line.get("technical_specification"),
                },
                "missing_data": line.get("missing_data") or [],
            })
        elif component_type == "glue":
            weight_g = line.get("weight_per_piece_g")
            rule_cost = (weight_g / 1000) * 100 if weight_g is not None else None
            requests.append({
                "component_type": "glue",
                "agent_to_use": None,
                "payload_type": "rule_based_cost",
                "payload": {
                    "material": "glue",
                    "weight_per_piece_g": weight_g,
                    "cost_eur_per_kg": 100,
                    "cost_per_piece": rule_cost,
                },
                "missing_data": line.get("missing_data") or [],
            })

    return {
        "component_cost_requests": requests,
        "warning": "External Component Costing Expert must only receive component payloads, not the full choke.",
    }


def find_costing_record_for_component(component_type, records):
    for record in records:
        if classify_choke_material(record) != component_type:
            continue
        cost = extract_cost_per_piece(record)
        if cost is not None:
            return record, cost
    return None, None


def calculate_material_cost(component_costing_json, normalized_bom):
    records = parse_external_costing_records(component_costing_json)
    lines = normalized_bom.get("component_lines", []) if isinstance(normalized_bom, dict) else []
    material_lines = []
    missing_component_costs = []
    total = 0.0

    for line in lines:
        component_type = line.get("component_type")
        cost_per_piece = None
        source = None
        matched_record = None

        if component_type in ["ferrite", "wire"]:
            matched_record, cost_per_piece = find_costing_record_for_component(
                component_type,
                records,
            )
            if cost_per_piece is not None:
                source = "external_component_costing_expert"
        elif component_type == "tin":
            cost_per_piece = extract_cost_per_piece(line.get("source_line"))
            if cost_per_piece is not None:
                source = "bom_cost_per_piece"
            else:
                price_per_kg = extract_price_per_kg(line.get("source_line"))
                weight_g = line.get("weight_per_piece_g")
                if price_per_kg is not None and weight_g is not None:
                    cost_per_piece = (weight_g / 1000) * price_per_kg
                    source = "material_price_lookup"
        elif component_type == "glue":
            weight_g = line.get("weight_per_piece_g")
            if weight_g is not None:
                cost_per_piece = (weight_g / 1000) * 100
                source = "rule_based_100_eur_per_kg"

        if cost_per_piece is None:
            missing_component_costs.append(component_type)
        else:
            total += cost_per_piece

        material_lines.append({
            "component_type": component_type,
            "cost_per_piece": cost_per_piece,
            "cost_source": source,
            "matched_costing_record": matched_record,
            "normalized_bom_line": line,
        })

    return {
        "material_cost_per_piece": total,
        "material_lines": material_lines,
        "missing_component_costs": list(dict.fromkeys(missing_component_costs)),
    }


def normalize_added_value_operations(operations_json):
    operations = parse_most_operations(operations_json)
    normalized_operations = []

    for index, operation in enumerate(operations, start=1):
        normalized = {
            "operation_id": operation.get("operation_id") or operation.get("id") or index,
            "operation_name": operation.get("operation_name") or operation.get("operation") or f"operation_{index}",
            "p_h": calculate_operation_produced_per_hour(operation),
            "oee": normalize_oee(operation_value(operation, ["oee", "oee_percent", "costing_oee_percent"])),
            "operator_percent": numeric_value(operation_value(operation, ["operator_percent", "percent_operator"])),
            "generic_capex_eur": numeric_value(operation_value(operation, ["generic_capex_eur", "generic_capex"])) or 0,
            "specific_capex_eur": numeric_value(operation_value(operation, ["specific_capex_eur", "specific_capex"])) or 0,
            "tooling_cost_eur": numeric_value(operation_value(operation, ["tooling_cost_eur", "tooling_cost"])),
            "tooling_life_pieces": numeric_value(operation_value(operation, ["tooling_life_pieces", "tooling_life_parts", "tooling_lifetime_parts"])),
            "tooling_adder_per_piece_eur": numeric_value(operation_value(operation, ["tooling_adder_per_piece_eur"])),
            "raw_operation": operation,
        }
        missing_data = []
        if normalized["p_h"] in [None, 0]:
            missing_data.append("p_h")
        if normalized["operator_percent"] is None:
            missing_data.append("operator_percent")
        normalized["missing_data"] = missing_data
        normalized_operations.append(normalized)

    return normalized_operations


def calculate_added_value_cost(operations, plant_data, commercial_data):
    return calculate_added_value_from_most(
        operations,
        plant_data,
        commercial_data,
    )


def build_choke_cost_summary(material_result, added_value_result):
    missing_inputs = []
    missing_inputs.extend(material_result.get("missing_component_costs") or [])
    missing_inputs.extend(added_value_result.get("missing_inputs") or [])

    for operation in added_value_result.get("operation_costs") or []:
        for missing in operation.get("missing_inputs") or []:
            missing_inputs.append(f"{operation.get('operation_name')}: {missing}")

    material_cost = material_result.get("material_cost_per_piece") or 0
    direct_labor_cost = added_value_result.get("direct_labor_cost_per_piece") or 0
    voh_cost = added_value_result.get("voh_cost_per_piece") or 0
    added_value_cost = added_value_result.get("added_value_cost_per_piece") or 0

    return {
        "material_cost_per_piece": material_cost,
        "direct_labor_cost_per_piece": direct_labor_cost,
        "voh_cost_per_piece": voh_cost,
        "added_value_cost_per_piece": added_value_cost,
        "preliminary_choke_cost_per_piece": material_cost + added_value_cost,
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
        "next_steps": [
            "apply packaging and transportation",
            "apply FOH and fees",
            "build PriceCalAVO structure",
            "run P&L / NPV",
        ],
    }


def load_records():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    agent_json_record_id,
                    project_code,
                    product_reference,
                    json_type,
                    source_agent,
                    validation_status,
                    created_at,
                    payload
                FROM agent_json_records
                ORDER BY agent_json_record_id DESC
                LIMIT 50
            """)
            return cur.fetchall()


ensure_table()
seed_default_test_scenarios()

tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    "RFQ DB Intake",
    "New validation",
    "Validation records",
    "Test Scenarios",
    "Costing Tests",
    "Choke Costing V1",
    "External Component Agent V1",
    "Choke Orchestrator Demo V1",
    "Choke Backend Orchestrator V1",
])

with tab0:
    st.header("RFQ DB Intake")
    st.info(
        "RFQ DB Intake creates the initial triage draft from Sales/RFQ structured data. "
        "The validation agent must still inspect attached files before release to costing."
    )

    if not RFQ_DATABASE_URL:
        st.warning(
            "RFQ_DATABASE_URL is not configured. You can still paste rfq_data JSON manually."
        )

    if "rfq_intake_json_text" not in st.session_state:
        st.session_state["rfq_intake_json_text"] = ""

    systematic_rfq_id = st.text_input(
        "systematic_rfq_id",
        placeholder="Example: 26240-ASS-00",
    )

    if st.button("Load RFQ from DB"):
        rfq_data, error = load_rfq_data_from_db(systematic_rfq_id.strip())

        if error:
            st.error(error)
        else:
            rfq_data.setdefault("systematic_rfq_id", systematic_rfq_id.strip())
            st.session_state["rfq_intake_json_text"] = json.dumps(
                rfq_data,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            st.success("RFQ data loaded from DB.")

    rfq_data_text = st.text_area(
        "Paste rfq_data JSON manually",
        key="rfq_intake_json_text",
        height=320,
    )

    if st.button("Convert RFQ data to validation draft"):
        if not rfq_data_text.strip():
            st.error("rfq_data JSON is required.")
        else:
            try:
                rfq_data = json.loads(rfq_data_text)
                if systematic_rfq_id.strip():
                    rfq_data.setdefault("systematic_rfq_id", systematic_rfq_id.strip())
                validation_draft = build_validation_from_rfq_data(rfq_data)
                st.session_state["rfq_validation_draft"] = validation_draft
                st.success("RFQ data converted to validation draft.")
            except json.JSONDecodeError as exc:
                st.error(f"Invalid rfq_data JSON: {exc}")
            except Exception as exc:
                st.error(f"Failed to convert RFQ data: {exc}")

    validation_draft = st.session_state.get("rfq_validation_draft")

    if validation_draft:
        st.subheader("Validation draft")
        st.json(validation_draft)

        if st.button("Store RFQ validation draft", type="primary"):
            try:
                project_code = normalize_project_code(extract_project_code(validation_draft))
                product_reference = extract_product_reference(validation_draft)

                if not project_code or project_code == "UNKNOWN_PROJECT":
                    st.error("Could not detect project/RFQ code from validation draft.")
                    st.stop()

                record_id = save_record(
                    project_code=project_code,
                    product_reference=product_reference,
                    json_type="project_validation",
                    source_agent="RFQ DB Intake",
                    validation_status=validation_draft["customer_input_validation"].get(
                        "package_status",
                        "draft",
                    ),
                    payload=validation_draft,
                )
                save_summary_and_issues(
                    record_id=record_id,
                    project_code=project_code,
                    product_reference=product_reference,
                    payload=validation_draft,
                )
                st.success(
                    f"RFQ validation draft stored successfully. Record ID: {record_id}"
                )
            except Exception as exc:
                st.error(f"Failed to store RFQ validation draft: {exc}")

with tab1:
    st.header("1. Customer input files")

    uploaded_files = st.file_uploader(
        "Upload customer input files",
        accept_multiple_files=True,
        type=["pdf", "xlsx", "xlsm", "xls", "step", "stp", "docx", "png", "jpg", "jpeg"],
    )

    if uploaded_files:
        st.success(f"{len(uploaded_files)} file(s) selected")
        for f in uploaded_files:
            st.write(f"- {f.name}")

    st.header("2. Agent validation result")

    st.info(
        "Run the manual validation agent, then paste its JSON result here. "
        "Later this step can be replaced by an automatic API call."
    )

    col1, col2 = st.columns(2)

    with col1:
        st.caption("Project code and product reference will be extracted from the validation JSON.")

        manual_project_code = st.text_input(
            "Optional override: Project / RFQ code",
            placeholder="Leave blank - extracted automatically"
        )

        manual_product_reference = st.text_input(
            "Optional override: Product reference",
            placeholder="Leave blank - extracted automatically"
        )
    with col2:
        json_type = st.selectbox(
            "JSON type",
            ["project_validation", "component_json", "bom_json", "router_operation_json", "costing_json"],
        )
        source_agent = st.text_input("Source agent", value="External Component Costing Expert")

    validation_status = st.selectbox(
        "Validation status",
        ["incomplete", "complete", "blocked", "draft"],
    )

    payload_text = st.text_area("Paste validation JSON here", height=350)

    if st.button("Store validation JSON", type="primary"):
        if not payload_text.strip():
            st.error("Validation JSON is required.")
        else:
            try:
                payload = json.loads(payload_text)

                extracted_project_code = extract_project_code(payload)
                extracted_product_reference = extract_product_reference(payload)
                extracted_customer = extract_customer(payload)

                final_project_code = (
                    normalize_project_code(manual_project_code.strip())
                    if manual_project_code.strip()
                    else normalize_project_code(extracted_project_code)
                )
                final_product_reference = manual_product_reference.strip() or extracted_product_reference

                if not final_project_code or final_project_code == "UNKNOWN_PROJECT":
                    st.error("Could not detect project/RFQ code from JSON. Please use the optional override.")
                    st.stop()

                record_id = save_record(
                    project_code=final_project_code,
                    product_reference=final_product_reference,
                    json_type=json_type.strip(),
                    source_agent=source_agent.strip() or None,
                    validation_status=validation_status,
                    payload=payload,
                )
                save_summary_and_issues(
                    record_id=record_id,
                    project_code=final_project_code,
                    product_reference=final_product_reference,
                    payload=payload,
                )
                summary = extract_summary(payload)

                st.success(f"Validation JSON stored successfully. Record ID: {record_id}")

                st.subheader("Detected package identity")
                st.json({
                    "project_code": final_project_code,
                    "customer": extracted_customer,
                    "product_reference": final_product_reference,
                    "json_type": json_type,
                    "validation_status": validation_status,
                })

                st.subheader("Validation summary")
                st.json(summary)

            except json.JSONDecodeError as exc:
                st.error(f"Invalid JSON: {exc}")
            except Exception as exc:
                st.error(f"Failed to store validation JSON: {exc}")

with tab2:
    st.header("Validation records")

    st.subheader("RFQ validation summary")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    rfq_validation_summary_id,
                    project_code,
                    customer,
                    product_reference,
                    package_status,
                    can_continue_to_costing,
                    missing_count,
                    blocking_missing_count,
                    next_agent,
                    created_at
                FROM rfq_validation_summary
                ORDER BY rfq_validation_summary_id DESC
                LIMIT 50
            """)
            summary_rows = cur.fetchall()

    if summary_rows:
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)
    else:
        st.info("No RFQ validation summary yet.")

    st.subheader("Detected products")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    project_code,
                    product_reference,
                    product_line,
                    product_family,
                    drawing_status,
                    max_quantity_status,
                    max_quantity_value,
                    delivery_zone,
                    target_price_status,
                    sop_status,
                    validation_status,
                    created_at
                FROM rfq_validation_products
                ORDER BY created_at DESC, rfq_validation_product_id DESC
                LIMIT 100
            """)
            product_rows = cur.fetchall()

    if product_rows:
        st.dataframe(pd.DataFrame(product_rows), use_container_width=True)
    else:
        st.info("No detected products yet.")

    st.subheader("Detected components")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.project_code,
                    COALESCE(c.product_reference, sp.product_reference) AS product_reference,
                    c.component_name,
                    c.component_code_or_drawing_number,
                    c.component_family,
                    c.quantity_status,
                    c.quantity_value,
                    c.drawing_status,
                    c.plant_to_deliver,
                    c.internal_or_external_status,
                    c.internal_or_external_value,
                    c.is_blocking,
                    c.validation_status,
                    c.created_at
                FROM rfq_validation_components c
                LEFT JOIN (
                    SELECT
                        agent_json_record_id,
                        project_code,
                        MAX(product_reference) AS product_reference
                    FROM rfq_validation_products
                    GROUP BY agent_json_record_id, project_code
                    HAVING COUNT(*) = 1
                ) sp
                    ON sp.agent_json_record_id = c.agent_json_record_id
                    AND sp.project_code = c.project_code
                ORDER BY c.is_blocking DESC, c.created_at DESC, c.rfq_validation_component_id DESC
                LIMIT 100
            """)
            component_rows = cur.fetchall()

    if component_rows:
        component_df = pd.DataFrame(component_rows)
        component_df["component_family"] = component_df.apply(
            lambda row: row["component_family"]
            if pd.notna(row["component_family"]) and row["component_family"]
            else infer_component_family(
                row.get("component_name"),
                row.get("component_code_or_drawing_number"),
            ),
            axis=1,
        )
        component_df["product_reference"] = component_df["product_reference"].fillna("(missing)")
        st.dataframe(component_df, use_container_width=True)
    else:
        st.info("No detected components yet.")

    st.subheader("Component Costing Orchestrator")

    project_codes = load_component_project_codes()
    selected_queue_project = None

    if project_codes:
        selected_queue_project = st.selectbox(
            "Project to prepare for component costing",
            project_codes,
        )

        if st.button("Generate component costing queue"):
            try:
                inserted_count = generate_component_costing_queue(selected_queue_project)
                st.success(
                    "Component costing queue generated for "
                    f"{selected_queue_project}: {inserted_count} component(s)."
                )
            except Exception as exc:
                st.error(f"Failed to generate component costing queue: {exc}")
    else:
        st.info("No validated components available for costing orchestration yet.")

    try:
        queue_rows = load_component_costing_queue(selected_queue_project)
        queue_summary = summarize_component_costing_queue(queue_rows)

        metric_cols = st.columns(5)
        metric_cols[0].metric("Total components", queue_summary["total_components"])
        metric_cols[1].metric(
            "Automatic external costing",
            queue_summary["automatic_external_costing"],
        )
        metric_cols[2].metric(
            "Internal costing required",
            queue_summary["internal_costing_required"],
        )
        metric_cols[3].metric(
            "Manual engineer required",
            queue_summary["manual_costing_engineer_required"],
        )
        metric_cols[4].metric(
            "Blocked missing data",
            queue_summary["blocked_missing_data"],
        )

        if queue_rows:
            queue_df = pd.DataFrame(queue_rows)
            queue_df["component_family"] = queue_df.apply(
                lambda row: row["component_family"]
                if pd.notna(row["component_family"]) and row["component_family"]
                else infer_component_family(
                    row.get("component_name"),
                    row.get("component_code_or_drawing_number"),
                ),
                axis=1,
            )
            queue_df["product_reference"] = queue_df["product_reference"].fillna("(missing)")
            st.dataframe(queue_df, use_container_width=True)
        else:
            st.info("No component costing queue rows yet.")
    except Exception as exc:
        st.error(f"Failed to load component costing queue: {exc}")

    st.subheader("Open validation issues")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    project_code,
                    product_reference,
                    issue_level,
                    issue_field,
                    is_blocking,
                    issue_status,
                    issue_description,
                    created_at
                FROM rfq_validation_issues
                WHERE issue_status = 'open'
                ORDER BY is_blocking DESC, created_at DESC
                LIMIT 100
            """)
            issue_rows = cur.fetchall()

    if issue_rows:
        st.dataframe(pd.DataFrame(issue_rows), use_container_width=True)
    else:
        st.info("No open validation issues yet.")

    st.subheader("Stored validation records")

    try:
        records = load_records()

        if not records:
            st.info("No validation records yet.")
        else:
            rows = []
            for r in records:
                summary = extract_summary(r["payload"])
                rows.append({
                    "ID": r["agent_json_record_id"],
                    "Project": r["project_code"],
                    "Product": r["product_reference"],
                    "Type": r["json_type"],
                    "Status": r["validation_status"],
                    "Can continue": summary.get("can_continue_to_costing"),
                    "Missing count": summary.get("missing_count"),
                    "Next agent": summary.get("next_agent"),
                    "Created": r["created_at"],
                })

            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)

            st.subheader("Payload viewer")

            selected_id = st.number_input("Open record ID", min_value=1, step=1)

            selected = next((r for r in records if r["agent_json_record_id"] == selected_id), None)

            if selected:
                st.subheader(f"Payload for record {selected_id}")
                st.json(selected["payload"])
            else:
                st.info("Enter one of the visible record IDs to inspect its payload.")

    except Exception as exc:
        st.error(f"Failed to load records: {exc}")

with tab3:
    st.header("Test Scenarios")
    st.caption(
        "Demo and validation tracker for the RFQ validation and component costing "
        "orchestration workflow. No AI API call or costing calculation is run here."
    )

    try:
        scenarios = load_test_scenarios()

        if not scenarios:
            st.info("No test scenarios available.")
        else:
            scenario_rows = [
                {
                    "ID": scenario["scenario_id"],
                    "Scenario": scenario["scenario_name"],
                    "Type": scenario["scenario_type"],
                    "Agent": scenario["agent_under_test"],
                    "Status": scenario["test_status"],
                    "Updated": scenario["updated_at"],
                }
                for scenario in scenarios
            ]
            st.dataframe(pd.DataFrame(scenario_rows), use_container_width=True)

            scenario_options = {
                f"{scenario['scenario_id']} - {scenario['scenario_name']}": scenario
                for scenario in scenarios
            }
            selected_label = st.selectbox(
                "Select scenario",
                list(scenario_options.keys()),
            )
            selected_scenario = scenario_options[selected_label]

            st.subheader(selected_scenario["scenario_name"])

            detail_cols = st.columns(3)
            detail_cols[0].metric("Scenario ID", selected_scenario["scenario_id"])
            detail_cols[1].metric("Type", selected_scenario["scenario_type"])
            detail_cols[2].metric("Status", selected_scenario["test_status"])

            st.text_input(
                "Agent under test",
                value=selected_scenario["agent_under_test"],
                disabled=True,
            )

            st.text_area(
                "Input required",
                value=selected_scenario["input_required"],
                height=120,
                disabled=True,
            )

            st.text_area(
                "Expected output",
                value=selected_scenario["expected_output"],
                height=120,
                disabled=True,
            )

            status_options = ["not_run", "passed", "failed", "blocked"]
            current_status = selected_scenario["test_status"] or "not_run"
            status_index = (
                status_options.index(current_status)
                if current_status in status_options
                else 0
            )

            test_status = st.selectbox(
                "Test status",
                status_options,
                index=status_index,
                key=f"scenario_status_{selected_scenario['scenario_id']}",
            )

            actual_output = st.text_area(
                "Actual output",
                value=selected_scenario["actual_output"] or "",
                height=180,
                key=f"scenario_actual_{selected_scenario['scenario_id']}",
            )

            notes = st.text_area(
                "Notes",
                value=selected_scenario["notes"] or "",
                height=120,
                key=f"scenario_notes_{selected_scenario['scenario_id']}",
            )

            if st.button(
                "Save result",
                type="primary",
                key=f"save_scenario_{selected_scenario['scenario_id']}",
            ):
                update_test_scenario_result(
                    scenario_id=selected_scenario["scenario_id"],
                    actual_output=actual_output,
                    test_status=test_status,
                    notes=notes,
                )
                st.success("Scenario result saved.")
                if hasattr(st, "rerun"):
                    st.rerun()
                else:
                    st.experimental_rerun()

    except Exception as exc:
        st.error(f"Failed to load test scenarios: {exc}")

with tab4:
    st.header("Costing Tests")
    st.caption(
        "R&D / purchasing runner for costing-ready external components. "
        "This prepares payloads and stores results only; it does not call an AI API "
        "or calculate costs in Streamlit."
    )

    if "component_costing_payload" not in st.session_state:
        st.session_state["component_costing_payload"] = None
    if "component_costing_source_data" not in st.session_state:
        st.session_state["component_costing_source_data"] = None
    if "component_costing_test_id" not in st.session_state:
        st.session_state["component_costing_test_id"] = None
    if "costing_workflow_route" not in st.session_state:
        st.session_state["costing_workflow_route"] = None

    scenario_name = st.text_input(
        "Scenario name",
        value="RFQ component costing test",
        key="costing_test_scenario_name",
    )
    costing_input_text = st.text_area(
        "Paste rfq_data JSON",
        height=280,
        key="costing_test_input_json",
        placeholder="Paste the CRM/RFQ rfq_data JSON here.",
    )
    expected_output = st.text_area(
        "Expected output",
        height=100,
        key="costing_test_expected_output",
        placeholder="Optional expected result for the test/demo.",
    )

    if st.button("Build costing payload"):
        if not costing_input_text.strip():
            st.error("rfq_data JSON is required.")
        else:
            try:
                rfq_data = json.loads(costing_input_text)
                costing_payload = build_external_component_costing_payload_from_rfq_data(
                    rfq_data
                )
                st.session_state["component_costing_payload"] = costing_payload
                st.session_state["component_costing_source_data"] = rfq_data
                st.session_state["component_costing_test_id"] = None
                st.success("Costing payload built.")
            except json.JSONDecodeError as exc:
                st.error(f"Invalid rfq_data JSON: {exc}")
            except Exception as exc:
                st.error(f"Failed to build costing payload: {exc}")

    costing_payload = st.session_state.get("component_costing_payload")

    if costing_payload:
        st.subheader("Generated costing payload")
        st.json(costing_payload)

        payload_text = json.dumps(
            costing_payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        st.text_area(
            "Copyable costing payload",
            value=payload_text,
            height=360,
            key=f"copyable_external_component_costing_payload_{abs(hash(payload_text))}",
        )

        if costing_payload.get("payload_status") == "requires_choke_decomposition_workflow":
            st.warning(
                "Do not send this complete choke payload directly to External "
                "Component Costing Expert. Send it first to Costing Workflow Router."
            )
        elif costing_payload.get("payload_status") == "incomplete_for_costing_agent":
            st.warning(
                "Payload is incomplete for the costing agent. Missing: "
                + ", ".join(costing_payload.get("missing_for_costing_agent") or [])
            )
        else:
            st.success("Payload is ready for the External Component Costing Expert.")

        if st.button("Save costing test input"):
            try:
                test_id = save_component_costing_test(
                    scenario_name=scenario_name.strip() or "RFQ component costing test",
                    input_payload=costing_payload,
                    expected_output=expected_output.strip() or None,
                    actual_output=None,
                    test_status="not_run",
                    source_data=st.session_state.get("component_costing_source_data"),
                )
                st.session_state["component_costing_test_id"] = test_id
                st.success(f"Costing test input saved. Test ID: {test_id}")
            except Exception as exc:
                st.error(f"Failed to save costing test input: {exc}")

        if costing_payload.get("payload_status") == "requires_choke_decomposition_workflow":
            st.info("Send this payload first to Costing Workflow Router.")
        else:
            st.info("Paste this payload into External Component Costing Expert.")

        actual_output_text = st.text_area(
            "Paste External Component Costing Expert JSON output",
            height=260,
            key="costing_test_actual_output",
        )
        test_status = st.selectbox(
            "Test status",
            ["not_run", "passed", "failed", "blocked"],
            key="costing_test_status",
        )

        if st.button("Save costing test result", type="primary"):
            try:
                actual_output = None
                if actual_output_text.strip():
                    actual_output = json.loads(actual_output_text)

                test_id = st.session_state.get("component_costing_test_id")
                if test_id:
                    update_component_costing_test_result(
                        component_costing_test_id=test_id,
                        actual_output=actual_output,
                        test_status=test_status,
                        expected_output=expected_output.strip() or None,
                    )
                else:
                    test_id = save_component_costing_test(
                        scenario_name=scenario_name.strip() or "RFQ component costing test",
                        input_payload=costing_payload,
                        expected_output=expected_output.strip() or None,
                        actual_output=actual_output,
                        test_status=test_status,
                        source_data=st.session_state.get("component_costing_source_data"),
                    )
                    st.session_state["component_costing_test_id"] = test_id

                st.success(f"Costing test result saved. Test ID: {test_id}")
                if hasattr(st, "rerun"):
                    st.rerun()
                else:
                    st.experimental_rerun()
            except json.JSONDecodeError as exc:
                st.error(f"Invalid External Component Costing Expert JSON output: {exc}")
            except Exception as exc:
                st.error(f"Failed to save costing test result: {exc}")

    st.subheader("Costing Workflow Router")
    st.caption(
        "Routes costing-ready payloads to the correct workflow before any costing "
        "agent is called."
    )

    default_workflow_payload_text = ""
    if costing_payload:
        default_workflow_payload_text = json.dumps(
            costing_payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    workflow_payload_text = st.text_area(
        "Paste generated costing payload JSON",
        value=default_workflow_payload_text,
        height=260,
        key=f"costing_workflow_payload_input_{abs(hash(default_workflow_payload_text))}",
    )

    if st.button("Build workflow route"):
        if not workflow_payload_text.strip():
            st.error("Generated costing payload JSON is required.")
        else:
            try:
                workflow_payload = json.loads(workflow_payload_text)
                workflow_route = build_costing_workflow_route_from_payload(workflow_payload)
                st.session_state["costing_workflow_route"] = workflow_route
                st.success("Costing workflow route built.")
            except json.JSONDecodeError as exc:
                st.error(f"Invalid generated costing payload JSON: {exc}")
            except Exception as exc:
                st.error(f"Failed to build costing workflow route: {exc}")

    workflow_route = st.session_state.get("costing_workflow_route")

    if workflow_route:
        st.json(workflow_route)

        if st.button("Save workflow route"):
            try:
                route_id = save_costing_workflow_route(workflow_route)
                st.success(f"Costing workflow route saved. Route ID: {route_id}")
                if hasattr(st, "rerun"):
                    st.rerun()
                else:
                    st.experimental_rerun()
            except Exception as exc:
                st.error(f"Failed to save costing workflow route: {exc}")

    st.subheader("Stored workflow routes")

    try:
        workflow_routes = load_costing_workflow_routes()
        if workflow_routes:
            st.dataframe(pd.DataFrame(workflow_routes), use_container_width=True)
        else:
            st.info("No costing workflow routes stored yet.")
    except Exception as exc:
        st.error(f"Failed to load costing workflow routes: {exc}")

    st.subheader("Choke Workflow V1")
    st.caption(
        "Builds the internal choke decomposition route from back-office parameters. "
        "No external costing agent or MOST agent is called."
    )

    choke_col1, choke_col2, choke_col3 = st.columns(3)
    with choke_col1:
        choke_project_code = st.text_input(
            "Project code",
            value="",
            key="choke_v1_project_code",
        )
        choke_product_name = st.text_input(
            "Product name",
            value="Rod Choke",
            key="choke_v1_product_name",
        )
        choke_delivery_zone = st.text_input(
            "Delivery zone",
            value="",
            key="choke_v1_delivery_zone",
        )
        choke_production_plant = st.text_input(
            "Production plant from manufacturing strategy",
            value="",
            key="choke_v1_production_plant",
        )

    with choke_col2:
        choke_annual_quantity = st.number_input(
            "Annual quantity",
            min_value=0,
            value=0,
            step=1000,
            key="choke_v1_annual_quantity",
        )
        choke_wire_diameter = st.number_input(
            "Wire diameter mm",
            min_value=0.0,
            value=1.18,
            step=0.01,
            format="%.3f",
            key="choke_v1_wire_diameter",
        )
        choke_turns = st.number_input(
            "Number of turns",
            min_value=0.0,
            value=11.0,
            step=1.0,
            format="%.2f",
            key="choke_v1_turns",
        )
        choke_tin_thickness = st.number_input(
            "Tin thickness micron",
            min_value=0.0,
            value=20.0,
            step=1.0,
            format="%.2f",
            key="choke_v1_tin_thickness",
        )

    with choke_col3:
        choke_ferrite_diameter = st.number_input(
            "Ferrite diameter mm",
            min_value=0.0,
            value=5.0,
            step=0.1,
            format="%.2f",
            key="choke_v1_ferrite_diameter",
        )
        choke_ferrite_length = st.number_input(
            "Ferrite length mm",
            min_value=0.0,
            value=16.0,
            step=0.1,
            format="%.2f",
            key="choke_v1_ferrite_length",
        )
        choke_left_direction_changes = st.number_input(
            "Left direction changes",
            min_value=0,
            value=0,
            step=1,
            key="choke_v1_left_direction_changes",
        )
        choke_right_direction_changes = st.number_input(
            "Right direction changes",
            min_value=0,
            value=0,
            step=1,
            key="choke_v1_right_direction_changes",
        )

    st.caption("Drawing analysis indicators")
    drawing_cols = st.columns(3)
    with drawing_cols[0]:
        choke_glue_mentioned = st.checkbox(
            "Glue mentioned",
            key="choke_v1_glue_mentioned",
        )
        choke_push_force_mentioned = st.checkbox(
            "Ferrite push out force mentioned",
            key="choke_v1_push_force_mentioned",
        )
    with drawing_cols[1]:
        choke_wire_crosses_faces = st.checkbox(
            "Wire crosses both flat faces",
            key="choke_v1_wire_crosses_faces",
        )
        choke_flat_faces_without_wire = st.checkbox(
            "One/both flat faces without wire",
            key="choke_v1_flat_faces_without_wire",
        )
    with drawing_cols[2]:
        choke_explicit_locked = st.checkbox(
            "Explicit locked",
            key="choke_v1_explicit_locked",
        )
        choke_explicit_glued = st.checkbox(
            "Explicit glued",
            key="choke_v1_explicit_glued",
        )

    if "choke_workflow_v1_output" not in st.session_state:
        st.session_state["choke_workflow_v1_output"] = None

    if st.button("Build Choke Workflow"):
        choke_input = {
            "project_code": choke_project_code.strip() or None,
            "product_name": choke_product_name.strip() or None,
            "delivery_zone": choke_delivery_zone.strip() or None,
            "production_plant": choke_production_plant.strip() or None,
            "manufacturing_strategy_source": (
                "manual_streamlit_input"
                if choke_production_plant.strip()
                else "not_provided"
            ),
            "annual_quantity": choke_annual_quantity,
            "wire_diameter_mm": choke_wire_diameter,
            "number_of_turns": choke_turns,
            "tin_thickness_micron": choke_tin_thickness,
            "ferrite_diameter_mm": choke_ferrite_diameter,
            "ferrite_length_mm": choke_ferrite_length,
            "left_direction_changes": choke_left_direction_changes,
            "right_direction_changes": choke_right_direction_changes,
            "drawing_analysis": {
                "glue_mentioned": choke_glue_mentioned,
                "ferrite_push_out_force_mentioned": choke_push_force_mentioned,
                "wire_crosses_both_flat_faces": choke_wire_crosses_faces,
                "one_or_both_flat_faces_without_wire": choke_flat_faces_without_wire,
                "explicit_locked": choke_explicit_locked,
                "explicit_glued": choke_explicit_glued,
            },
        }
        st.session_state["choke_workflow_v1_output"] = build_choke_workflow_v1(
            choke_input
        )
        st.success("Choke Workflow V1 route built.")

    if st.session_state.get("choke_workflow_v1_output"):
        st.json(st.session_state["choke_workflow_v1_output"])

    st.subheader("Stored costing tests")

    try:
        costing_tests = load_component_costing_tests()
        if costing_tests:
            st.dataframe(pd.DataFrame(costing_tests), use_container_width=True)
        else:
            st.info("No costing tests stored yet.")
    except Exception as exc:
        st.error(f"Failed to load costing tests: {exc}")

with tab5:
    st.header("Choke Costing V1")
    st.caption(
        "Preliminary choke cost build from pasted agent outputs: material + direct "
        "labor + VOH. No GPT API call, no MCP write, and no database write is run here."
    )

    st.subheader("Choke Costing Chain V1")
    st.caption(
        "End-to-end preliminary chain: customer/RFQ summary, BOM normalization, "
        "component cost requests, material cost, added value cost, and preliminary summary."
    )

    chain_customer_input_text = st.text_area(
        "Customer input JSON",
        height=180,
        key="choke_chain_customer_input_json",
        placeholder="Paste customer input / RFQ JSON.",
    )
    chain_bom_text = st.text_area(
        "Choke BOM Analyzer JSON",
        height=220,
        key="choke_chain_bom_json",
        placeholder="Paste Choke BOM Analyzer output.",
    )
    chain_component_costing_text = st.text_area(
        "Component costing JSON",
        height=220,
        key="choke_chain_component_costing_json",
        placeholder="Paste component costing outputs for ferrite, wire, tin and/or glue.",
    )
    chain_operations_text = st.text_area(
        "Added value operations JSON",
        height=220,
        key="choke_chain_operations_json",
        placeholder="Paste MOST Assemblage operation JSON.",
    )

    st.caption("Plant / commercial parameters")
    chain_col1, chain_col2, chain_col3, chain_col4 = st.columns(4)
    with chain_col1:
        chain_annual_quantity = st.number_input(
            "Chain annual_quantity",
            min_value=0.0,
            value=0.0,
            step=1000.0,
            key="choke_chain_annual_quantity",
        )
        chain_production_plant = st.text_input(
            "Chain production_plant",
            value="",
            key="choke_chain_production_plant",
        )
    with chain_col2:
        chain_operating_currency = st.text_input(
            "Chain operating_currency",
            value="",
            key="choke_chain_operating_currency",
        )
        chain_selling_currency = st.text_input(
            "Chain selling_currency",
            value="EUR",
            key="choke_chain_selling_currency",
        )
    with chain_col3:
        chain_fx_rate = st.number_input(
            "Chain fx_rate",
            min_value=0.0,
            value=1.0,
            step=0.01,
            format="%.6f",
            key="choke_chain_fx_rate",
        )
        chain_direct_labor_rate = st.number_input(
            "Chain direct labor rate / hour",
            min_value=0.0,
            value=0.0,
            step=1.0,
            key="choke_chain_direct_labor_rate",
        )
    with chain_col4:
        chain_voh_rate = st.number_input(
            "Chain VOH rate / hour",
            min_value=0.0,
            value=0.0,
            step=1.0,
            key="choke_chain_voh_rate",
        )
        chain_plant_open_hours = st.number_input(
            "Chain plant open hours / year",
            min_value=0.0,
            value=0.0,
            step=100.0,
            key="choke_chain_plant_open_hours",
        )

    if "choke_costing_chain_v1_output" not in st.session_state:
        st.session_state["choke_costing_chain_v1_output"] = None

    if st.button("Build Choke Costing Chain V1", type="primary"):
        try:
            customer_input_summary = build_choke_customer_input_from_rfq(
                chain_customer_input_text,
            )
            normalized_bom = normalize_choke_bom(chain_bom_text)
            component_cost_requests = build_component_cost_requests(normalized_bom)
            normalized_operations = normalize_added_value_operations(chain_operations_text)
            plant_data = {
                "production_plant": chain_production_plant.strip() or None,
                "operating_currency": chain_operating_currency.strip() or None,
                "selling_currency": chain_selling_currency.strip() or None,
                "fx_rate_operating_to_selling": chain_fx_rate,
                "direct_labor_rate_operating_currency_per_hour": chain_direct_labor_rate,
                "voh_rate_operating_currency_per_hour": chain_voh_rate,
                "plant_open_hours_per_year": chain_plant_open_hours,
            }
            annual_quantity = (
                chain_annual_quantity
                or numeric_value(customer_input_summary.get("annual_quantity"))
                or 0
            )
            commercial_data = {
                "annual_quantity": annual_quantity,
                "currency": customer_input_summary.get("currency"),
                "target_price": customer_input_summary.get("target_price"),
                "sop_year": customer_input_summary.get("sop_year"),
            }
            material_result = calculate_material_cost(
                chain_component_costing_text,
                normalized_bom,
            )
            added_value_result = calculate_added_value_cost(
                normalized_operations,
                plant_data,
                commercial_data,
            )
            preliminary_summary = build_choke_cost_summary(
                material_result,
                added_value_result,
            )
            st.session_state["choke_costing_chain_v1_output"] = {
                "chain_type": "choke_costing_chain_v1",
                "warning": (
                    "This is preliminary choke cost only. The full choke must not "
                    "be sent to External Component Costing Expert."
                ),
                "customer_input_summary": customer_input_summary,
                "normalized_bom": normalized_bom,
                "component_cost_requests": component_cost_requests,
                "material_cost_result": material_result,
                "normalized_added_value_operations": normalized_operations,
                "added_value_result": added_value_result,
                "preliminary_choke_cost_summary": preliminary_summary,
            }
            st.success("Choke Costing Chain V1 built.")
        except json.JSONDecodeError as exc:
            st.error(f"Invalid pasted JSON in Choke Costing Chain V1: {exc}")
        except Exception as exc:
            st.error(f"Failed to build Choke Costing Chain V1: {exc}")

    if st.session_state.get("choke_costing_chain_v1_output"):
        chain_output = st.session_state["choke_costing_chain_v1_output"]
        st.subheader("Customer input summary")
        st.json(chain_output["customer_input_summary"])
        st.subheader("Normalized BOM")
        st.json(chain_output["normalized_bom"])
        st.subheader("Component cost requests")
        st.json(chain_output["component_cost_requests"])
        st.subheader("Material cost result")
        st.json(chain_output["material_cost_result"])
        st.subheader("Added value result")
        st.json(chain_output["added_value_result"])
        st.subheader("Preliminary choke cost summary")
        st.json(chain_output["preliminary_choke_cost_summary"])
        st.subheader("Full chain JSON")
        st.json(chain_output)

    st.subheader("Legacy Choke Costing V1 calculator")

    bom_json_text = st.text_area(
        "Choke BOM JSON",
        height=240,
        key="choke_costing_bom_json",
        placeholder="Paste Choke BOM Analyzer JSON output.",
    )
    external_costing_json_text = st.text_area(
        "External component costing JSONs",
        height=220,
        key="choke_costing_external_jsons",
        placeholder="Paste External Component Costing Expert output JSON, or a list of outputs.",
    )
    most_operations_json_text = st.text_area(
        "MOST operations JSON",
        height=220,
        key="choke_costing_most_json",
        placeholder="Paste MOST Assemblage operation JSON output.",
    )

    st.subheader("Plant data")
    plant_col1, plant_col2, plant_col3 = st.columns(3)
    with plant_col1:
        direct_labor_rate = st.number_input(
            "Direct labor rate operating currency per hour",
            min_value=0.0,
            value=0.0,
            step=1.0,
            key="choke_costing_dl_rate",
        )
        voh_rate = st.number_input(
            "VOH rate operating currency per hour",
            min_value=0.0,
            value=0.0,
            step=1.0,
            key="choke_costing_voh_rate",
        )
    with plant_col2:
        plant_open_hours = st.number_input(
            "Plant open hours per year",
            min_value=0.0,
            value=0.0,
            step=100.0,
            key="choke_costing_plant_open_hours",
        )
        fx_rate = st.number_input(
            "FX rate operating_to_selling",
            min_value=0.0,
            value=1.0,
            step=0.01,
            format="%.6f",
            key="choke_costing_fx_rate",
        )
    with plant_col3:
        operating_currency = st.text_input(
            "Operating currency",
            value="",
            key="choke_costing_operating_currency",
        )
        selling_currency = st.text_input(
            "Selling currency",
            value="EUR",
            key="choke_costing_selling_currency",
        )

    st.subheader("Commercial data")
    commercial_col1, commercial_col2, commercial_col3 = st.columns(3)
    with commercial_col1:
        choke_costing_annual_quantity = st.number_input(
            "Annual quantity",
            min_value=0.0,
            value=0.0,
            step=1000.0,
            key="choke_costing_annual_quantity",
        )
    with commercial_col2:
        sop_year = st.number_input(
            "SOP year",
            min_value=0,
            value=0,
            step=1,
            key="choke_costing_sop_year",
        )
    with commercial_col3:
        initial_price = st.number_input(
            "Initial price if available",
            min_value=0.0,
            value=0.0,
            step=0.01,
            format="%.6f",
            key="choke_costing_initial_price",
        )

    if "choke_costing_v1_output" not in st.session_state:
        st.session_state["choke_costing_v1_output"] = None

    if st.button("Calculate Choke Costing V1", type="primary"):
        try:
            plant_data = {
                "direct_labor_rate_operating_currency_per_hour": direct_labor_rate,
                "voh_rate_operating_currency_per_hour": voh_rate,
                "plant_open_hours_per_year": plant_open_hours,
                "operating_currency": operating_currency.strip() or None,
                "selling_currency": selling_currency.strip() or None,
                "fx_rate_operating_to_selling": fx_rate,
            }
            commercial_data = {
                "annual_quantity": choke_costing_annual_quantity,
                "sop_year": sop_year or None,
                "initial_price": initial_price if initial_price > 0 else None,
            }
            material_result = calculate_material_cost_from_bom(
                bom_json_text,
                external_costing_json_text,
            )
            added_value_result = calculate_added_value_from_most(
                most_operations_json_text,
                plant_data,
                commercial_data,
            )
            total_result = calculate_total_choke_cost(
                material_result,
                added_value_result,
            )
            st.session_state["choke_costing_v1_output"] = {
                "costing_type": "preliminary_choke_cost_build",
                "warning": "This is not final offer pricing. It is material + DL + VOH only.",
                "plant_data": plant_data,
                "commercial_data": commercial_data,
                "material_result": material_result,
                "added_value_result": added_value_result,
                "total_result": total_result,
            }
            st.success("Choke Costing V1 calculated.")
        except json.JSONDecodeError as exc:
            st.error(f"Invalid pasted JSON: {exc}")
        except Exception as exc:
            st.error(f"Failed to calculate Choke Costing V1: {exc}")

    if st.session_state.get("choke_costing_v1_output"):
        st.json(st.session_state["choke_costing_v1_output"])

with tab6:
    st.header("External Component Agent V1")
    st.caption(
        "Backend-callable dry-run wrapper for the External Component Costing Agent. "
        "It validates scope, selects the costing prompt family, and prepares the prompt/save address."
    )

    default_payload_path = BASE_DIR / "data" / "test_payloads" / "external_component_ferrite_3165001.json"
    default_component_payload = ""
    if default_payload_path.exists():
        default_component_payload = default_payload_path.read_text(encoding="utf-8")

    component_payload_text = st.text_area(
        "Component payload JSON",
        value=default_component_payload,
        height=360,
        key="external_component_agent_payload",
    )
    dry_run = st.checkbox(
        "Dry run",
        value=True,
        key="external_component_agent_dry_run",
    )

    if st.button("Run Agent", type="primary", key="run_external_component_agent"):
        try:
            component_payload = json.loads(component_payload_text or "{}")
            agent_result = run_external_component_agent(component_payload, dry_run=dry_run)
            st.session_state["external_component_agent_result"] = agent_result
            if agent_result.get("status") == "blocked":
                st.warning("External Component Agent blocked this payload.")
            else:
                st.success("External Component Agent payload is ready.")
        except json.JSONDecodeError as exc:
            st.session_state["external_component_agent_result"] = None
            st.error(f"Invalid component payload JSON: {exc}")
        except Exception as exc:
            st.session_state["external_component_agent_result"] = None
            st.error(f"Failed to run External Component Agent: {exc}")

    agent_result = st.session_state.get("external_component_agent_result")
    if agent_result:
        st.subheader("Validation result")
        st.json(agent_result.get("validation") or {})

        summary_col1, summary_col2, summary_col3 = st.columns(3)
        with summary_col1:
            st.write("Classified family")
            st.code(agent_result.get("classified_family") or "")
        with summary_col2:
            st.write("Selected prompt")
            st.code(agent_result.get("selected_prompt_file") or "")
        with summary_col3:
            st.write("Save address")
            st.code(agent_result.get("save_address") or "")

        st.subheader("Prompt or output")
        if agent_result.get("call_structure"):
            st.json(agent_result)
        else:
            st.text_area(
                "Prompt to send",
                value=agent_result.get("prompt_to_send") or "",
                height=360,
                key="external_component_agent_prompt_to_send",
            )

with tab7:
    st.header("Choke Orchestrator Demo V1")
    st.caption(
        "Demo-ready orchestration: manufacturing strategy, plant data, planned "
        "BOM/component/MOST calls, and DL/VOH calculation in one standard envelope."
    )

    default_fuse_payload = {
        "project_code": "24003-CHO-00",
        "product_line": "Chokes",
        "product": "Fuse chokes",
        "product_id": "316-5001",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "drawing_reference": "316-5001-1-\u00e7\u2020\u201d\u00e6\u2013\u00ad\u00e7\u201d\u00b5\u00e6\u201e\u0178-QS198102-0051 customer confirmed.pdf",
    }
    rod_europe_payload = {
        "project_code": "demo-rod-europe",
        "product_line": "Chokes",
        "product": "Rod choke",
        "product_id": "demo-rod-choke",
        "customer_delivery_zone": "Europe",
        "annual_quantity": 1000000,
        "drawing_reference": "demo-rod-choke-customer-confirmed.pdf",
    }

    def set_choke_orchestrator_fields(demo_payload):
        st.session_state["choke_orch_project_code"] = demo_payload["project_code"]
        st.session_state["choke_orch_product_line"] = demo_payload["product_line"]
        st.session_state["choke_orch_product"] = demo_payload["product"]
        st.session_state["choke_orch_product_id"] = demo_payload["product_id"]
        st.session_state["choke_orch_customer_delivery_zone"] = demo_payload["customer_delivery_zone"]
        st.session_state["choke_orch_annual_quantity"] = int(demo_payload["annual_quantity"])
        st.session_state["choke_orch_drawing_reference"] = demo_payload["drawing_reference"]

    if "choke_orch_project_code" not in st.session_state:
        set_choke_orchestrator_fields(default_fuse_payload)
    if "choke_orch_result" not in st.session_state:
        st.session_state["choke_orch_result"] = None

    example_col1, example_col2 = st.columns(2)
    with example_col1:
        if st.button("Demo Fuse choke China South Pacific", key="choke_orch_demo_fuse"):
            set_choke_orchestrator_fields(default_fuse_payload)
            st.session_state["choke_orch_result"] = build_choke_workspace_orchestration(
                default_fuse_payload,
                dry_run=st.session_state.get("choke_orch_dry_run", True),
            )
    with example_col2:
        if st.button("Demo Rod choke Europe", key="choke_orch_demo_rod"):
            set_choke_orchestrator_fields(rod_europe_payload)
            st.session_state["choke_orch_result"] = build_choke_workspace_orchestration(
                rod_europe_payload,
                dry_run=st.session_state.get("choke_orch_dry_run", True),
            )

    form_col1, form_col2, form_col3 = st.columns(3)
    with form_col1:
        orch_project_code = st.text_input("project_code", key="choke_orch_project_code")
        orch_product_line = st.text_input("product_line", key="choke_orch_product_line")
        orch_product = st.text_input("product", key="choke_orch_product")
    with form_col2:
        orch_product_id = st.text_input("product_id", key="choke_orch_product_id")
        orch_delivery_zone = st.text_input(
            "customer_delivery_zone",
            key="choke_orch_customer_delivery_zone",
        )
        orch_annual_quantity = st.number_input(
            "annual_quantity",
            min_value=0,
            step=10000,
            key="choke_orch_annual_quantity",
        )
    with form_col3:
        orch_dry_run = st.checkbox(
            "dry_run",
            value=True,
            key="choke_orch_dry_run",
        )
        orch_drawing_reference = st.text_area(
            "drawing_reference",
            height=90,
            key="choke_orch_drawing_reference",
        )

    if st.button("Run Choke Orchestrator", type="primary", key="run_choke_orchestrator_demo"):
        try:
            orchestrator_payload = {
                "project_code": orch_project_code,
                "product_line": orch_product_line,
                "product": orch_product,
                "product_id": orch_product_id,
                "customer_delivery_zone": orch_delivery_zone,
                "annual_quantity": orch_annual_quantity,
                "drawing_reference": orch_drawing_reference,
            }
            st.session_state["choke_orch_result"] = build_choke_workspace_orchestration(
                orchestrator_payload,
                dry_run=orch_dry_run,
            )
            st.success("Choke Orchestrator Demo V1 completed.")
        except Exception as exc:
            st.session_state["choke_orch_result"] = None
            st.error(f"Failed to run Choke Orchestrator Demo V1: {exc}")

    choke_orch_result = st.session_state.get("choke_orch_result")
    if choke_orch_result:
        st.subheader("Selected manufacturing strategy")
        st.json(choke_orch_result.get("manufacturing_strategy") or {})

        st.subheader("Plant data")
        st.json(choke_orch_result.get("plant_data") or {})

        st.subheader("Planned agent calls")
        planned_calls = (
            (choke_orch_result.get("agent_outputs") or {}).get("planned_calls")
            or []
        )
        if planned_calls:
            st.dataframe(pd.DataFrame(planned_calls), use_container_width=True)
        else:
            st.info("No planned calls were generated.")

        st.subheader("DL/VOH calculation result")
        st.json(choke_orch_result.get("financial_calculation") or {})

        st.subheader("Standardized JSON")
        st.json(choke_orch_result)

with tab8:
    st.header("Choke Backend Orchestrator V1")
    st.caption(
        "Temporary backend-first test console. Customer input goes first; "
        "Streamlit only displays the standardized JSON envelope."
    )

    default_backend_input = {
        "project_code": "24003-CHO-00",
        "customer": "Zhejiang NBT",
        "final_customer": "BYD",
        "product_line": "Chokes",
        "product": "Fuse choke",
        "product_id": "316-5001",
        "part_number": "316-5001",
        "drawing_reference": "316-5001-1-customer-confirmed.pdf",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "currency": "RMB",
        "target_price": 1.5,
        "sop_date": None,
    }

    if "choke_backend_input_json" not in st.session_state:
        st.session_state["choke_backend_input_json"] = json.dumps(
            default_backend_input,
            indent=2,
        )
    if "choke_backend_result" not in st.session_state:
        st.session_state["choke_backend_result"] = None

    backend_input_text = st.text_area(
        "Customer input JSON",
        height=320,
        key="choke_backend_input_json",
    )
    backend_col1, backend_col2 = st.columns(2)
    with backend_col1:
        backend_dry_run = st.checkbox(
            "dry_run",
            value=True,
            key="choke_backend_dry_run",
        )
    with backend_col2:
        backend_trigger_agents = st.checkbox(
            "trigger_agents",
            value=False,
            key="choke_backend_trigger_agents",
        )

    if st.button("Run Choke Backend Orchestrator", type="primary"):
        try:
            backend_customer_input = json.loads(backend_input_text or "{}")
            st.session_state["choke_backend_result"] = run_choke_orchestration(
                backend_customer_input,
                dry_run=backend_dry_run,
                trigger_agents=backend_trigger_agents,
            )
            st.success("Choke Backend Orchestrator V1 completed.")
        except json.JSONDecodeError as exc:
            st.session_state["choke_backend_result"] = None
            st.error(f"Invalid customer input JSON: {exc}")
        except Exception as exc:
            st.session_state["choke_backend_result"] = None
            st.error(f"Failed to run Choke Backend Orchestrator V1: {exc}")

    backend_result = st.session_state.get("choke_backend_result")
    if backend_result:
        st.subheader("Manufacturing strategy")
        st.json(backend_result.get("manufacturing_strategy") or {})

        st.subheader("Unit data")
        st.json(backend_result.get("unit_data") or {})

        st.subheader("Component agent calls")
        st.json((backend_result.get("agent_orchestration") or {}).get("component_agent_calls") or [])

        st.subheader("MOST component-operation calls")
        st.json((backend_result.get("agent_orchestration") or {}).get("most_agent_calls") or [])

        st.subheader("Financial calculation")
        st.json(backend_result.get("financial_calculation") or {})

        st.subheader("Standard JSON")
        st.json(backend_result)
