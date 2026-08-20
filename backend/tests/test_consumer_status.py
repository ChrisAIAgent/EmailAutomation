import json
from datetime import datetime, timedelta, timezone

from app import consumer_status


def test_consumer_status_reports_fresh_heartbeat(monkeypatch, tmp_path):
    status_path = tmp_path / "consumer-status.json"
    monkeypatch.setattr(consumer_status, "DATA_DIR", tmp_path)
    monkeypatch.setattr(consumer_status, "STATUS_PATH", status_path)

    consumer_status.write_consumer_status("running")
    status = consumer_status.read_consumer_status()

    assert status["healthy"] is True
    assert status["state"] == "running"
    assert status["pid"]


def test_consumer_status_rejects_stale_heartbeat(monkeypatch, tmp_path):
    status_path = tmp_path / "consumer-status.json"
    monkeypatch.setattr(consumer_status, "STATUS_PATH", status_path)
    status_path.write_text(
        json.dumps(
            {
                "state": "running",
                "pid": 123,
                "heartbeat_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=10)
                ).isoformat(),
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    status = consumer_status.read_consumer_status(max_age_seconds=150)

    assert status["healthy"] is False
    assert status["age_seconds"] >= 590
