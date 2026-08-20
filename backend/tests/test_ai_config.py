import json
from unittest.mock import patch

from app import models


def _email_payload(**overrides):
    data = {
        "provider_name": "openai-compatible",
        "base_url": "https://provider.example/v1",
        "model": "example-model",
        "api_key": "test-secret-key",
    }
    data.update(overrides)
    return data


def test_email_ai_config_is_encrypted_and_read_api_never_returns_key(client, db):
    saved = client.put("/api/agent-profile/email-ai-config", json=_email_payload())
    assert saved.status_code == 200
    assert saved.json()["api_key_configured"] is True
    assert "api_key" not in saved.json()
    row = db.get(models.SystemFlag, "email_ai_config")
    assert row is not None
    assert "test-secret-key" not in row.value
    read = client.get("/api/agent-profile/email-ai-config")
    assert read.status_code == 200
    assert "api_key" not in read.json()


def test_ai_config_rejects_unsafe_provider_urls(client):
    for url in ("http://provider.example/v1", "ftp://provider.example/v1", "https://user:pass@provider.example/v1"):
        response = client.put("/api/agent-profile/email-ai-config", json=_email_payload(base_url=url))
        assert response.status_code == 422


def test_ai_connection_uses_explicit_target_and_redacts_result(client):
    fake = type("Response", (), {"status_code": 200})()
    with patch("httpx.post", return_value=fake) as request:
        response = client.post("/api/agent-profile/email-ai-config/test", json=_email_payload())
    assert response.status_code == 200
    assert response.json()["target"] == "https://provider.example/v1"
    assert "test-secret-key" not in json.dumps(response.json())
    assert request.call_args.args[0] == "https://provider.example/v1/chat/completions"
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer test-secret-key"


def test_legacy_unified_flag_is_migrated(client, db):
    # Seed the old single unified flag; it should migrate into email_ai_config
    # and be deleted. Any leftover tacwork split flag is also cleaned up.
    from app.security import get_cipher
    legacy = {
        "provider_name": "openai-compatible",
        "base_url": "https://legacy.example/v1",
        "model": "legacy-model",
        "api_key": "legacy-key",
        "share_with_tacwork": False,
        "tacwork_base_url": "https://legacy-tac.example/v1",
        "tacwork_model": "legacy-tac-model",
        "tacwork_api_key": "legacy-tac-key",
    }
    db.add(models.SystemFlag(key="unified_ai_config", value=get_cipher().encrypt(json.dumps(legacy))))
    db.commit()
    email = client.get("/api/agent-profile/email-ai-config")
    assert email.status_code == 200
    assert email.json()["model"] == "legacy-model"
    assert db.get(models.SystemFlag, "unified_ai_config") is None
    assert db.get(models.SystemFlag, "tacwork_ai_config") is None
