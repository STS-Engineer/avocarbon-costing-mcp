"""Build component-scoped physical inputs for one MOST work package."""

from typing import Any, Dict, List


def _first(rows: List[Dict[str, Any]], *keys: str) -> Any:
    for row in rows:
        for key in keys:
            value = row.get(key)
            if value not in (None, "", []):
                return value
    return None


def build_physical_operation_scope(
    work_package: Dict[str, Any],
    technical_by_component: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    rows = list(technical_by_component.values())
    text = " ".join(
        str(work_package.get(key) or "")
        for key in ("operation_key", "operation_name")
    ).lower()
    common = {
        "component_technical_inputs": technical_by_component,
        "required_most_output_fields": [
            "operation_description",
            "process_steps",
            "equipment",
            "pieces_per_hour",
            "cycle_time_seconds",
            "oee",
            "operator_percent",
            "number_of_operators",
            "investment_tooling",
            "energy",
            "assumptions",
            "confirmation_questions",
        ],
    }
    if "winding" in text:
        return {
            **common,
            "physical_operation": (
                "Wire and ferrite handling, winding on the ferrite rod, "
                "and terminal-leg forming."
            ),
            "ferrite_rod": {
                "diameter_mm": _first(rows, "ferrite_diameter_mm", "core_diameter_mm"),
                "length_mm": _first(rows, "ferrite_length_mm", "core_length_mm", "length_mm"),
            },
            "enameled_wire": {
                "diameter_mm": _first(rows, "wire_diameter_mm", "diameter_mm"),
                "turns": _first(rows, "turns", "total_turns", "turns_total"),
                "developed_length_m_per_product": _first(
                    rows, "developed_length_m_per_product", "developed_length_m"
                ),
                "terminal_leg_1_mm": _first(
                    rows, "terminal_leg_1_mm", "leg_1_mm", "left_leg_mm"
                ),
                "terminal_leg_2_mm": _first(
                    rows, "terminal_leg_2_mm", "leg_2_mm", "right_leg_mm"
                ),
            },
            "process_steps": [
                "Load and orient ferrite rod and enameled wire",
                "Position wire and ferrite for winding",
                "Wind specified turns",
                "Form both terminal legs",
                "Unload and transfer the wound part",
            ],
        }
    if "glue" in text or "adhesive" in text:
        return {
            **common,
            "physical_operation": (
                "Ferrite retention by adhesive loading, positioning, "
                "dispensing, and handling."
            ),
            "adhesive": {
                "product": _first(
                    rows, "adhesive", "glue_product", "product", "designation"
                ),
                "deposit_count": _first(
                    rows, "application_count", "deposit_count", "glue_zones"
                ) or 2,
                "curing_requirement": _first(
                    rows, "curing_requirement", "curing", "baking"
                ),
            },
            "process_steps": [
                "Load and position wound part",
                "Prepare adhesive dispensing",
                "Apply the required deposits",
                "Handle and transfer without disturbing ferrite retention",
                "Include curing only when confirmed as part of this operation",
            ],
        }
    if "tin" in text or "solder" in text:
        return {
            **common,
            "physical_operation": (
                "Terminal preparation and tinning using the confirmed current process."
            ),
            "process_steps": [
                "Load and orient terminals",
                "Prepare terminal surface when required",
                "Apply confirmed tinning process",
                "Inspect coverage and unload",
            ],
        }
    return common
