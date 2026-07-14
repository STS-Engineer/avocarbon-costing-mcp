import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_sequential_agent_workflow import normalize_bom


def main():
    raw_bom = {
        "bill_of_material": [
            {
                "poste": "Ferrite",
                "produit_designation": "Ferrite pour choke",
                "quantite": 1,
                "specification": "Composition Fe/Ni ou Fe/Mg à confirmer",
            },
            {
                "poste": "Fil",
                "produit_designation": "Fil cuivre bobiné/isolé pour choke",
                "quantite": 1,
            },
            {
                "poste": "Etamage",
                "produit_designation": "Etain sur pattes dénudées",
                "quantite": 1,
            },
            {
                "poste": "Colle",
                "produit_designation": "Colle de maintien ferrite",
                "quantite": None,
                "remarques": "Impossible de conclure absence de colle; à confirmer.",
            },
            {
                "poste": "Ferrite",
                "produit_designation": "Duplicate ferrite description",
                "quantite": 1,
            },
        ]
    }
    normalized = normalize_bom(raw_bom)
    component_ids = [item["component_id"] for item in normalized["components"]]
    assert component_ids == [
        "ferrite_core",
        "magnet_wire",
        "lead_tinning",
        "glue",
    ], component_ids
    assert len(component_ids) == len(set(component_ids)), component_ids
    print("PASS French BOM component extraction is canonical and deduplicated")
    print(component_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
