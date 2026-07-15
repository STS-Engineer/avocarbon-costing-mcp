from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, workflow

try:
    project, product = create_most_ready_workflow()
    normalized = workflow._load_normalized_bom(project, product)
    glue = next(item for item in normalized["components"] if item["component_id"] == "glue")
    glue["status"] = "to_confirm"
    workflow._write_json(workflow._bom_normalized_path(project, product), normalized)
    calls = install_most_trigger_spy()
    result = workflow.trigger_most_operations(project, product, dry_run=True)
    triggered_ids = [call["payload"]["work_package_id"] for call in calls]
    assert "wp_40_glue_application_baking" not in triggered_ids, calls
    assert result["state"]["most"]["wp_40_glue_application_baking"]["status"] == "blocked", result
    assert "wp_40_glue_application_baking" not in result["state"]["required_most_work_package_ids"], result
    print("PASS: unconfirmed glue scope is blocked and not triggered")
finally:
    cleanup()
