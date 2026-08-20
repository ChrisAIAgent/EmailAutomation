from app.tasks import huey, validate_task_registry


def test_huey_registry_contains_worker_and_periodic_tasks():
    validate_task_registry()

    registered = huey._registry._registry
    assert "app.tasks.execute_automation_run" in registered
    assert "app.tasks.periodic_reclaim" in registered
    assert "app.tasks.periodic_scan" in registered
    assert "app.tasks.periodic_heartbeat" in registered
