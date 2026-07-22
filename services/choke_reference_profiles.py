import re
from copy import deepcopy
from typing import Any, Dict, List, Optional


REFERENCE_PROFILES: List[Dict[str, Any]] = [
    {
        "reference_id": "316-5001",
        "product_subtype": "fuse_choke",
        "applicability": {
            "exact_part_number": "316-5001",
            "customer": "Zhejiang NBT",
            "drawing_revision": None,
            "allowed_as_generic_default": False,
        },
        "validated_operations": [
            {"operation_code": "OP10", "operation_name": "Winding", "p_h": 450, "oee": 1.0, "operator_percent": 0.15},
            {"operation_code": "OP20", "operation_name": "Assembly core", "p_h": 600, "oee": 1.0, "operator_percent": 1.0},
            {"operation_code": "OP30", "operation_name": "Auto soldering", "p_h": 600, "oee": 1.0, "operator_percent": 0.5},
            {"operation_code": "OP40", "operation_name": "Inspection and packaging", "p_h": 10000, "oee": 1.0, "operator_percent": 1.0},
        ],
        "validated_exclusions": [
            "independent_ferrite_handling",
            "glue_application",
            "curing_baking",
            "electrical_test",
            "push_out_pull_test",
        ],
        "source": "customer_workbook:data/24003-CHO-00 - NBT  - Fuse  Chokes - assy Quotation.xlsm:Product Added value",
        "approval_status": "benchmark_only",
    }
]


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def find_reference_profile(
    part_number: Any,
    product_subtype: str,
    customer: Optional[str] = None,
) -> Dict[str, Any]:
    part_key = _key(part_number)
    exact = None
    comparables = []
    for profile in REFERENCE_PROFILES:
        subtype_match = profile["product_subtype"] == product_subtype
        exact_part = _key(profile["applicability"].get("exact_part_number")) == part_key and bool(part_key)
        expected_customer = _key(profile["applicability"].get("customer"))
        customer_match = not customer or not expected_customer or _key(customer) == expected_customer
        if subtype_match and exact_part and customer_match:
            exact = deepcopy(profile)
            break
        if subtype_match:
            comparable = deepcopy(profile)
            comparable["similarity"] = 0.5 + (0.35 if exact_part else 0.0)
            comparable["reuse_allowed"] = False
            comparable["comparison_reason"] = "Same Choke subtype; not an approved exact project match."
            comparables.append(comparable)
    return {
        "exact_match": exact,
        "exact_reference_id": exact.get("reference_id") if exact else None,
        "comparable_references": comparables,
    }

