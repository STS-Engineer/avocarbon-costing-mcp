from component_step2_test_support import cleanup, create_workflow, successful_raw, workflow

try:
    project, product = create_workflow()
    raw = successful_raw("ferrite_core")
    raw["recommended_offer"].update({"transportation_cost": 0.005, "custom_duty_cost": 0.001, "forwarder_cost": 0.0005})
    workflow.save_component_output(project, product, "ferrite_core", raw)
    output = workflow.get_component_output(project, product, "ferrite_core")
    normalized = output["normalized_component"]
    assert normalized["schema_version"] == "1.0", normalized
    assert normalized["classification"] == "External", normalized
    assert normalized["quantity_per_product"] == 1, normalized
    assert normalized["recommended_offer"]["origin"] == "China", normalized
    assert normalized["recommended_offer"]["transportation_cost_per_piece"] == 0.005, normalized
    print("PASS: component Agent output is normalized to schema 1.0")
finally:
    cleanup()
