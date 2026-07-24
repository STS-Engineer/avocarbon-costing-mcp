"""Read the existing product profitability objective without reinterpreting it."""

from __future__ import annotations

import os
from typing import Any, Dict

from sqlalchemy import create_engine, text


def get_product_profitability_objective(
    product_name: Any = None,
    product_id: Any = None,
) -> Dict[str, Any]:
    """Return the app-owned product target and its unresolved business semantics."""
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        return {
            "status": "not_available",
            "source_field": "products.roce_target_percent",
            "message": "DATABASE_URL is not configured.",
        }

    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            exists = connection.execute(text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'products'
                      AND column_name = 'roce_target_percent'
                )
            """)).scalar()
            if not exists:
                return {
                    "status": "not_available",
                    "source_field": "products.roce_target_percent",
                    "message": "The existing product profitability field is unavailable.",
                }
            row = connection.execute(
                text("""
                    SELECT product_id, product_name, roce_target_percent
                    FROM public.products
                    WHERE (
                        :product_name IS NOT NULL
                        AND product_name ILIKE :product_name
                    ) OR (
                        :product_id IS NOT NULL
                        AND CAST(product_id AS text) = :product_id
                    )
                    ORDER BY CASE WHEN product_name ILIKE :product_name THEN 0 ELSE 1 END
                    LIMIT 1
                """),
                {
                    "product_name": (
                        str(product_name).strip()
                        if product_name not in (None, "") else None
                    ),
                    "product_id": (
                        str(product_id).strip()
                        if product_id not in (None, "") else None
                    ),
                },
            ).mappings().first()
        engine.dispose()
    except Exception as exc:
        return {
            "status": "not_available",
            "source_field": "products.roce_target_percent",
            "message": f"Product profitability lookup failed: {exc}",
        }

    if not row or row.get("roce_target_percent") in (None, ""):
        return {
            "status": "not_found",
            "source_field": "products.roce_target_percent",
            "product_name": product_name,
            "product_id": product_id,
        }
    return {
        "status": "found_ambiguous",
        "source_table": "public.products",
        "source_field": "products.roce_target_percent",
        "source_product_id": row["product_id"],
        "source_product_name": row["product_name"],
        "value": float(row["roce_target_percent"]),
        "unit": "percent",
        "target_type": "ROCE",
        "target_interpretation": None,
        "blocking_business_decision": (
            "The product master stores a ROCE target, but no approved rule maps "
            "that percentage to an NPV residual. Confirm the interpretation "
            "before solving a firm selling price."
        ),
    }
