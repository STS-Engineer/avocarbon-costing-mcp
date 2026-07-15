from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, workflow

try:
    project, product = create_most_ready_workflow()
    calls = install_most_trigger_spy()
    result = workflow.trigger_most_operations(project, product, dry_run=True)
    expected = ["wp_10_ferrite_handling", "wp_20_wire_winding", "wp_30_lead_tinning", "wp_40_glue_application_baking", "wp_50_electrical_test", "wp_60_visual_inspection_packaging"]
    assert [call["payload"]["work_package_id"] for call in calls] == expected, calls
    assert all(call["agent_env"] == "CHATGPT_MOST_AGENT_ID" for call in calls), calls
    assert all(call["conversation_key"] == f"{project}:{product}:most:{call['payload']['work_package_id']}:v1" for call in calls), calls
    assert result["status"] == "most_triggered", result
    print("PASS: exactly one MOST Agent run is created per supported work package")
finally:
    cleanup()
