from services.choke_classification import classify_choke
from services.choke_process_decomposition import decompose_choke_process
from services.choke_process_routing import build_choke_process_route
from services.external_component_agent import classify_component_family, validate_component_payload
from services import choke_sequential_agent_workflow as workflow
from services import choke_orchestrator as orchestrator


def component(component_id, name, quantity=1, status="confirmed", **extra):
    raw = {
        "component_id": component_id,
        "component_name": name,
        "quantity_per_product": quantity,
        "status": status,
        **extra,
    }
    return raw


def normalize(raw, customer):
    classification = classify_choke(customer, raw)
    return workflow.normalize_bom(raw, customer, classification), classification


def route(raw, customer, policy=None):
    normalized, classification = normalize(raw, customer)
    return build_choke_process_route(customer, normalized, classification, policy)


def operation_keys(result):
    return [item["operation_key"] for item in result["operations"]]


def test_exact_3165001_uses_only_validated_workbook_profile():
    customer = {
        "customer": "Zhejiang NBT",
        "product": "Fuse Choke",
        "part_number": "316-5001",
    }
    raw = {
        "source": {"part_no": "316-5001", "drawing_title": "Fuse Choke"},
        "bom": [
            component("ferrite_core", "Ferrite core"),
            component("magnet_wire", "Magnet wire", 0.33),
            component("lead_tinning", "Tin material", 0.003),
            component("glue", "Glue", 0, status="not_confirmed"),
        ],
    }
    result = route(raw, customer)

    assert result["choke_subtype"] == "fuse_choke"
    assert result["exact_historical_profile_match"] == "316-5001"
    assert [(item["operation_code"], item["operation_name"], item["p_h"], item["operator_percent"]) for item in result["operations"]] == [
        ("OP10", "Winding", 450, 0.15),
        ("OP20", "Assembly core", 600, 1.0),
        ("OP30", "Auto soldering", 600, 0.5),
        ("OP40", "Inspection and packaging", 10000, 1.0),
    ]
    assert all(item["status"] == "confirmed" for item in result["operations"])
    assert all(item["rate_provenance"]["exact_reference_match"] is True for item in result["operations"])
    excluded = {item["operation_key"] for item in result["excluded_operations"]}
    assert {"independent_ferrite_handling", "glue_application", "curing_baking", "electrical_test", "push_out_pull_test"} <= excluded


def test_different_fuse_choke_does_not_inherit_3165001_rates_or_complete_route():
    customer = {"product": "Fuse Choke", "part_number": "NEW-FUSE-1"}
    raw = {"product_name": "Fuse Choke", "components": [
        component("ferrite_core", "Ferrite core"),
        component("magnet_wire", "Magnet wire", 0.25),
    ]}
    result = route(raw, customer)

    assert result["exact_historical_profile_match"] is None
    assert operation_keys(result) == ["wire_winding", "core_assembly"]
    assert result["operations"][0]["status"] == "confirmed"
    assert result["operations"][0].get("p_h") is None
    assert result["operations"][1]["status"] == "needs_confirmation"
    assert result["comparable_references"][0]["reuse_allowed"] is False


def test_confirmed_glue_and_curing_create_operations_but_unconfirmed_glue_does_not():
    customer = {"product": "Fuse Choke", "part_number": "NEW-FUSE-GLUE"}
    confirmed = {"product_name": "Fuse Choke", "curing_requirement": "Bake adhesive in oven", "components": [
        component("glue", "Epoxy glue", 0.001, status="confirmed"),
    ]}
    result = route(confirmed, customer)
    assert {"glue_application", "curing_baking"} <= set(operation_keys(result))

    unconfirmed = {"product_name": "Fuse Choke", "components": [
        component("glue", "Epoxy glue", 0.001, status="not_confirmed"),
    ]}
    result = route(unconfirmed, customer)
    assert "glue_application" not in operation_keys(result)
    assert "curing_baking" not in operation_keys(result)


def test_explicit_inductance_test_is_selected_and_absence_does_not_invent_it():
    customer = {"product": "Fuse Choke", "part_number": "NEW-FUSE-TEST"}
    base_components = [
        component("ferrite_core", "Ferrite core"),
        component("magnet_wire", "Magnet wire", 0.2),
    ]
    explicit = route({
        "product_name": "Fuse Choke",
        "test_requirement": "Measure inductance test at end of line",
        "components": base_components,
    }, customer)
    assert "inductance_test" in operation_keys(explicit)

    absent = route({"product_name": "Fuse Choke", "components": base_components}, customer)
    assert "inductance_test" not in operation_keys(absent)
    assert "electrical_test" not in operation_keys(absent)


def test_rod_and_toroid_keep_distinct_subtypes_and_evidence_driven_routes():
    for product, expected, expected_candidate in [
        ("Rod Choke", "rod_choke", "terminal_forming"),
        ("Toroid Choke", "toroid_choke", "ferrite_preparation"),
    ]:
        customer = {"product": product, "part_number": f"NEW-{expected}"}
        raw = {"product_name": product, "components": [
            component("ferrite_core", "Ferrite core"),
            component("magnet_wire", "Magnet wire", 0.4),
        ]}
        result = route(raw, customer)
        assert result["choke_subtype"] == expected
        assert result["exact_historical_profile_match"] is None
        assert operation_keys(result) == ["wire_winding"]
        assert result["operations"][0].get("p_h") is None
        assert expected_candidate in result["subtype_knowledge"]["candidate_operation_keys"]
        assert result["subtype_knowledge"]["validation_questions"]


def test_unknown_choke_never_defaults_and_returns_blocking_question():
    result = route({"components": [component("magnet_wire", "Magnet wire", 0.2)]}, {"product": "New magnetic device"})
    assert result["choke_subtype"] == "unknown_choke"
    assert result["status"] == "blocked"
    assert result["work_packages"] == []
    assert result["blocking_questions"]


def test_complete_chokes_rejected_but_bought_components_are_accepted():
    for complete_type in ["fuse_choke", "rod_choke", "toroid_choke"]:
        payload = {
            "component_type": complete_type,
            "component_definition": {"description": f"Complete {complete_type}"},
            "annual_quantity": 1000,
            "destination_zone": "Europe",
            "save_address": "data/result.json",
        }
        assert classify_component_family(payload) == "complete_choke"
        assert validate_component_payload(payload)["status"] == "blocked"

    cases = {
        "ferrite_core": "ferrite_component",
        "magnet_wire": "enameled_wire",
        "lead_tinning": "external_consumable",
        "glue": "external_consumable",
        "packaging": "packaging_component",
    }
    for component_type, expected in cases.items():
        payload = {
            "component_type": component_type,
            "component_definition": {"description": component_type, "parent_product": "Fuse Choke"},
            "annual_quantity": 1000,
            "destination_zone": "Europe",
            "save_address": "data/result.json",
        }
        assert classify_component_family(payload) == expected
        assert validate_component_payload(payload)["status"] == "ready_for_agent_call"


def test_routing_consistency_and_operation_provenance_across_entry_paths():
    customer = {"product": "Rod Choke", "part_number": "ROD-NEW"}
    raw = {"product_name": "Rod Choke", "components": [
        component("ferrite_core", "Ferrite rod"),
        component("magnet_wire", "Magnet wire", 0.4),
    ]}
    normalized, classification = normalize(raw, customer)
    direct = build_choke_process_route(customer, normalized, classification)
    state = {"project_code": "P", "product_id": "X", "customer_input": customer, "choke_classification": classification}
    sequential = workflow.build_most_process_decomposition(state, normalized)
    compatibility = decompose_choke_process(normalized, customer)

    assert [item["operation_key"] for item in direct["operations"]] == [item["operation_key"] for item in sequential["operations"]]
    assert [item["operation_key"] for item in direct["operations"]] == [item["operation_key"] for item in compatibility["operations"]]
    for operation in direct["operations"]:
        assert operation["reason_selected"]
        assert operation["evidence"]
        assert all(item["source_type"] and item["confidence"] for item in operation["evidence"])


def test_classification_trace_reaches_all_legacy_orchestrator_agent_payloads():
    customer = {
        "project_code": "P",
        "product_id": "X",
        "product": "Rod Choke",
        "annual_quantity": 1000,
        "customer_delivery_zone": "Europe",
    }
    classification = classify_choke(customer, {})
    bom_call = orchestrator._build_bom_agent_call(customer, "bom.json", True, False, classification)
    component_call = orchestrator._build_component_agent_call(
        customer,
        {"plant": "SAME", "selling_currency": "EUR"},
        {"component_id": "ferrite_core", "component_type": "ferrite_component", "bom_definition": {}},
        "ferrite.json",
        True,
        False,
        classification,
    )
    most_call = orchestrator._build_most_agent_call(
        customer,
        {"plant": "SAME"},
        {
            "work_package_id": "wp_10_wire_winding",
            "component_id": "magnet_wire",
            "operation_id": "OP10",
            "operation_name": "Winding",
        },
        "most.json",
        True,
        False,
        classification,
    )

    assert '"choke_subtype": "rod_choke"' in bom_call["input_text"]
    assert component_call["input_payload"]["choke_subtype"] == "rod_choke"
    assert most_call["input_payload"]["choke_subtype"] == "rod_choke"
