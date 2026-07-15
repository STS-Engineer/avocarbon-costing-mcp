from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, successful_most, workflow

try:
    project, product = create_most_ready_workflow()
    install_most_trigger_spy()
    first = workflow.trigger_most_operations(project, product, dry_run=True)
    first_id = first["process_decomposition"]["required_work_package_ids"][0]
    workflow.save_most_output(project, product, first_id, successful_most(first_id))
    calls = install_most_trigger_spy()
    second = workflow.trigger_most_operations(project, product, dry_run=True)
    assert first_id not in [call["payload"]["work_package_id"] for call in calls], calls
    assert {item["work_package_id"] for item in second["skipped_work_packages"]} >= {first_id}, second
    print("PASS: received MOST outputs are not retriggered")
finally:
    cleanup()
