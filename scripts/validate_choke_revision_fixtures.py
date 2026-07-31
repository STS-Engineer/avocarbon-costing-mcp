"""Validate revision reconciliation with two structurally different chokes."""

import json
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import choke_technical_revisions as revisions  # noqa: E402


def component(identifier, quantity, unit, price_unit, price):
    return {
        "component_id": identifier,
        "component": identifier.replace("_", " ").title(),
        "quantity_per_product": quantity,
        "quantity_unit": unit,
        "costing_route": "external_component_costing_agent",
        "component_definition": {
            "quantity_per_product": quantity,
            "quantity_unit": unit,
        },
        "_offer": {"pricing_unit": price_unit, "unit_price": price},
    }


def validate(name, components, operations, obsolete_id=None):
    raw = {"bom": components, "product_type": name}
    normalized = revisions.attach_bom_revisions(
        raw,
        {
            "components": components,
            "choke_classification": {"choke_subtype": name},
        },
    )
    process = revisions.attach_process_revisions(
        {
            "required_work_package_ids": [item["work_package_id"] for item in operations],
            "work_packages": operations,
        },
        normalized["technical_revision"],
        normalized,
    )
    state = {"components": {}, "most": {}}
    if obsolete_id:
        state["most"][obsolete_id] = {"status": "received"}
    most = revisions.reconcile_most_outputs(process, state, lambda _: None)
    unresolved = []
    preliminary_cost = Decimal("0")
    for item in normalized["components"]:
        offer = item["_offer"]
        quantity = revisions.resolve_technical_quantity(
            item, offer["pricing_unit"], mode="preliminary"
        )
        if quantity["resolution_status"] == "blocked":
            unresolved.append(item["component_id"])
        else:
            preliminary_cost += (
                Decimal(str(quantity["quantity"]))
                * Decimal(str(offer["unit_price"]))
            )
    return {
        "fixture": name,
        "detected_components": [item["component_id"] for item in components],
        "required_work_packages": most["required_work_packages"],
        "excluded_or_obsolete_outputs": [
            item["work_package_id"] for item in most["obsolete_work_packages"]
        ],
        "unresolved_inputs": unresolved,
        "preliminary_material_cost": float(preliminary_cost),
        "firm_readiness": "blocked" if unresolved or most["missing_work_packages"] else "ready",
    }


def main():
    structures = [
        validate(
            "rod_choke",
            [
                component("piece_material", 1, "pc", "pc", 0.2),
                component("mass_consumable", 0.4, "g", "kg", 8),
            ],
            [
                {
                    "work_package_id": "axial_winding",
                    "operation_key": "winding",
                    "component_ids": ["piece_material"],
                    "status": "confirmed",
                },
                {
                    "work_package_id": "material_application",
                    "operation_key": "application",
                    "component_ids": ["mass_consumable"],
                    "status": "confirmed",
                },
            ],
            obsolete_id="removed_operation",
        ),
        validate(
            "fuse_choke",
            [
                component("piece_material", 2, "pc", "pc", 0.1),
                component("length_material", 0.3, "m", "m", 0.5),
            ],
            [
                {
                    "work_package_id": "coil_forming",
                    "operation_key": "winding",
                    "component_ids": ["length_material"],
                    "status": "confirmed",
                },
                {
                    "work_package_id": "terminal_joining",
                    "operation_key": "soldering",
                    "component_ids": ["piece_material", "length_material"],
                    "status": "confirmed",
                },
            ],
        ),
    ]
    print(json.dumps(structures, indent=2))


if __name__ == "__main__":
    main()
