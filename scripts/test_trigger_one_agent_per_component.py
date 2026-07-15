from component_step2_test_support import cleanup, create_workflow, install_trigger_spy, workflow

try:
    project, product = create_workflow()
    calls = install_trigger_spy()
    result = workflow.trigger_next_component_costing(project, product, dry_run=True)
    assert result["status"] == "component_agents_triggered", result
    assert [call["payload"]["component_id"] for call in calls] == ["ferrite_core", "magnet_wire", "lead_tinning"], calls
    assert all(len([call["payload"]["component_id"]]) == 1 for call in calls)
    assert all(call["agent_env"] == "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID" for call in calls)
    print("PASS: exactly one External Component Agent trigger per routed BOM component")
finally:
    cleanup()
