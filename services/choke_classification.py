import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional


CHOKE_FAMILY = "choke"
CHOKE_SUBTYPES = {"fuse_choke", "rod_choke", "toroid_choke", "unknown_choke"}


def _match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(r"[^a-z0-9]+", " ", text.encode("ascii", "ignore").decode().lower()).strip()


def _part_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _match_text(value))


def _values(data: Any, keys: Iterable[str]) -> List[str]:
    wanted = {key.lower() for key in keys}
    found: List[str] = []

    def visit(value: Any, inside_bom_line: bool = False) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key).lower()
                next_inside = inside_bom_line or key_text in {
                    "components", "bom", "line_items", "bill_of_material", "materials"
                }
                if key_text in wanted and not inside_bom_line and nested not in (None, "", [], {}):
                    if isinstance(nested, (str, int, float)):
                        found.append(str(nested))
                visit(nested, next_inside)
        elif isinstance(value, list):
            for item in value:
                visit(item, inside_bom_line)

    visit(data)
    return list(dict.fromkeys(found))


def _evidence(source_type: str, source_reference: str, text: str, confidence: str) -> Dict[str, str]:
    return {
        "source_type": source_type,
        "source_reference": source_reference,
        "evidence_text": text,
        "confidence": confidence,
    }


def classify_choke(
    customer_input: Optional[Dict[str, Any]] = None,
    bom: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    customer_input = customer_input or {}
    bom = bom or {}
    raw_product = (
        customer_input.get("product")
        or customer_input.get("product_name")
        or next(iter(_values(bom, ["product", "product_name", "product_type", "choke_type"])), None)
    )
    part_number = (
        customer_input.get("part_number")
        or customer_input.get("drawing_number")
        or next(iter(_values(bom, ["part_number", "part_no", "drawing_number", "drawing_no"])), None)
    )
    drawing_title = next(iter(_values(bom, ["drawing_title", "title", "product_description"])), None)

    evidence_values = [value for value in [raw_product, part_number, drawing_title] if value not in (None, "")]
    evidence_text = " ".join(_match_text(value) for value in evidence_values)
    part = _part_key(part_number)
    matches: Dict[str, List[Dict[str, str]]] = {
        "fuse_choke": [],
        "rod_choke": [],
        "toroid_choke": [],
    }

    if part.startswith("3165001"):
        matches["fuse_choke"].append(
            _evidence("historical_reference", "validated_reference:316-5001", str(part_number), "confirmed")
        )
    if any(term in evidence_text for term in ["fuse choke", "choke fuse"]):
        matches["fuse_choke"].append(_evidence("specification", "product identity", str(raw_product), "high"))
    if "self avec fil emaille" in evidence_text and "barre ferrite" in evidence_text:
        matches["fuse_choke"].append(_evidence("drawing", "drawing title", str(drawing_title or raw_product), "high"))
    if any(term in evidence_text for term in ["rod choke", "choke rod", "ferrite rod choke"]):
        matches["rod_choke"].append(_evidence("specification", "product identity", str(raw_product or drawing_title), "high"))
    if any(term in evidence_text for term in ["toroid choke", "torroid choke", "toroidal choke"]):
        matches["toroid_choke"].append(_evidence("specification", "product identity", str(raw_product or drawing_title), "high"))

    resolved = [subtype for subtype, items in matches.items() if items]
    questions: List[str] = []
    if len(resolved) == 1:
        subtype = resolved[0]
        evidence = matches[subtype]
        confidence = "confirmed" if any(item["confidence"] == "confirmed" for item in evidence) else "high"
        source = evidence[0]["source_reference"]
        status = "resolved"
    else:
        subtype = "unknown_choke"
        evidence = [item for items in matches.values() for item in items]
        confidence = "low"
        source = "unresolved"
        status = "ambiguous" if len(resolved) > 1 else "unresolved"
        questions.append(
            "Confirm whether this product is a Fuse Choke, Rod Choke, Toroid Choke, or another Choke variant."
        )

    return {
        "status": status,
        "choke_family": CHOKE_FAMILY,
        "choke_subtype": subtype,
        "raw_detected_product_name": raw_product,
        "part_number": part_number,
        "classification_evidence": evidence,
        "classification_source": source,
        "confidence": confidence,
        "unresolved_classification_questions": questions,
        "candidate_subtypes": resolved,
    }


def classification_trace(classification: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    classification = classification or {}
    return {
        "choke_family": classification.get("choke_family") or CHOKE_FAMILY,
        "choke_subtype": classification.get("choke_subtype") or "unknown_choke",
        "raw_detected_product_name": classification.get("raw_detected_product_name"),
        "classification_evidence": classification.get("classification_evidence") or [],
        "classification_source": classification.get("classification_source") or "unresolved",
        "classification_confidence": classification.get("confidence") or "low",
        "unresolved_classification_questions": classification.get("unresolved_classification_questions") or [],
    }

