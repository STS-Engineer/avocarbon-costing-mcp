import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_sequential_agent_workflow import _build_bom_trigger_payload


def main():
    result = _build_bom_trigger_payload(
        "RFQ-MINIMAL-PAYLOAD",
        "PART-001",
        {
            "drawing_file_url": "https://example.test/drawing.pdf?sig=test",
            "drawing_reference": "DRAWING-001",
            "customer": "Must not be embedded",
            "annual_quantity": 999999,
        },
    )
    payload = result["payload"]
    expected_keys = {
        "project_code",
        "product_id",
        "drawing_file_url",
        "drawing_reference",
        "save_address",
        "instruction",
    }
    assert set(payload) == expected_keys, payload
    assert json.loads(result["input_text"]) == payload
    assert "customer_input" not in result["input_text"]
    assert "annual_quantity" not in result["input_text"]
    assert len(result["input_text"].encode("utf-8")) < 1000
    print("PASS BOM trigger input is the minimal six-field JSON payload")


if __name__ == "__main__":
    main()
