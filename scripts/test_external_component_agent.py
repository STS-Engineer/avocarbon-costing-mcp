import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.external_component_agent import run_external_component_agent


TEST_PAYLOADS = [
    ROOT_DIR / "data" / "test_payloads" / "external_component_ferrite_3165001.json",
    ROOT_DIR / "data" / "test_payloads" / "external_component_wire_3165001_raw_material_only.json",
]


def load_payload(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    for payload_path in TEST_PAYLOADS:
        payload = load_payload(payload_path)
        result = run_external_component_agent(payload, dry_run=True)
        prompt = result.get("prompt_to_send") or ""

        print(f"Payload: {payload_path.name}")
        print(f"validation status: {result.get('status')}")
        print(f"classified family: {result.get('classified_family')}")
        print(f"selected prompt file: {result.get('selected_prompt_file')}")
        print(f"save address: {result.get('save_address')}")
        print("prompt_to_send first 500 characters:")
        print(prompt[:500])
        print("-" * 80)


if __name__ == "__main__":
    main()
