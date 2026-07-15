from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, successful_most, workflow

try:
    project, product = create_most_ready_workflow()
    install_most_trigger_spy()
    triggered = workflow.trigger_most_operations(project, product, dry_run=True)
    ids = triggered["process_decomposition"]["required_work_package_ids"]
    result = workflow.save_most_output(project, product, ids[0], successful_most(ids[0]))
    assert result["state"]["most"][ids[0]]["status"] == "received", result
    assert all(result["state"]["most"][item]["status"] == "triggered" for item in ids[1:]), result
    assert result["remaining_work_packages"] == ids[1:], result
    print("PASS: one MOST write-back preserves all other scope states")
finally:
    cleanup()
