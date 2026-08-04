from services.choke_most_input_contract import build_physical_operation_scope
from services.choke_sequential_agent_workflow import _most_trigger_payload


def test_winding_scope_contains_complete_physical_inputs():
    scope = build_physical_operation_scope(
        {"operation_key": "wire_winding", "operation_name": "Winding"},
        {
            "ferrite_core": {
                "ferrite_diameter_mm": 3,
                "ferrite_length_mm": 13,
            },
            "magnet_wire": {
                "wire_diameter_mm": 0.75,
                "turns": 14.5,
                "terminal_leg_1_mm": 5,
                "terminal_leg_2_mm": 7,
            },
        },
    )
    assert scope["ferrite_rod"] == {"diameter_mm": 3, "length_mm": 13}
    assert scope["enameled_wire"]["diameter_mm"] == 0.75
    assert scope["enameled_wire"]["turns"] == 14.5
    assert scope["enameled_wire"]["terminal_leg_1_mm"] == 5
    assert scope["enameled_wire"]["terminal_leg_2_mm"] == 7
    assert "wire and ferrite handling" in scope["physical_operation"].lower()
    assert "number_of_operators" in scope["required_most_output_fields"]


def test_glue_scope_has_two_deposits_and_conditional_curing():
    scope = build_physical_operation_scope(
        {"operation_key": "glue_application", "operation_name": "Glue application"},
        {"glue": {"glue_product": "EP-138", "application_count": 2}},
    )
    assert scope["adhesive"]["product"] == "EP-138"
    assert scope["adhesive"]["deposit_count"] == 2
    assert any("curing only when confirmed" in item.lower() for item in scope["process_steps"])


def test_most_payload_preserves_work_package_and_current_factory_inputs():
    payload = _most_trigger_payload(
        {
            "project_code": "24018-CHO-00",
            "product_id": "300440157",
            "unit_data": {"plant": "Chennai"},
        },
        {
            "work_package_id": "wp_10_wire_winding",
            "operation_id": "OP 10",
            "operation_name": "Winding",
            "component_ids": ["ferrite_core", "magnet_wire"],
            "technical_inputs": {"turns": 14.5},
            "annual_quantity": 360000,
            "production_plant": "Chennai",
        },
        "trigger-1",
    )
    assert payload["technical_inputs"] == {"turns": 14.5}
    assert payload["annual_quantity"] == 360000
    assert payload["production_plant"] == "Chennai"
    assert payload["trigger_run_id"] == "trigger-1"

