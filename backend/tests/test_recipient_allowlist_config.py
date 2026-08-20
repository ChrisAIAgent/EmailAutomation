from app.config import Settings


def test_restricted_allowlist_empty_means_unrestricted():
    settings = Settings(
        _env_file=None,
        RESTRICTED_RECIPIENT_ALLOWLIST="",
        TEST_RECIPIENT_ALLOWLIST=None,
    )
    assert settings.recipient_allowlist == set()


def test_restricted_allowlist_normalizes_recipients():
    settings = Settings(
        _env_file=None,
        RESTRICTED_RECIPIENT_ALLOWLIST=" Lead@Example.com, second@example.com ",
        TEST_RECIPIENT_ALLOWLIST=None,
    )
    assert settings.recipient_allowlist == {"lead@example.com", "second@example.com"}


def test_legacy_test_allowlist_is_used_only_as_fallback():
    settings = Settings(
        _env_file=None,
        RESTRICTED_RECIPIENT_ALLOWLIST="",
        TEST_RECIPIENT_ALLOWLIST="legacy@example.com",
    )
    assert settings.recipient_allowlist == {"legacy@example.com"}


def test_restricted_allowlist_wins_over_legacy_alias():
    settings = Settings(
        _env_file=None,
        RESTRICTED_RECIPIENT_ALLOWLIST="current@example.com",
        TEST_RECIPIENT_ALLOWLIST="legacy@example.com",
    )
    assert settings.recipient_allowlist == {"current@example.com"}


def test_dashboard_exposes_only_restricted_allowlist_key(client, monkeypatch):
    from app.api import dashboard

    monkeypatch.setattr(
        dashboard,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            RESTRICTED_RECIPIENT_ALLOWLIST="allowed@example.com",
            TEST_RECIPIENT_ALLOWLIST=None,
        ),
    )
    payload = client.get("/api/dashboard/metrics").json()
    assert payload["restricted_allowlist_configured"] is True
    assert "test_allowlist_configured" not in payload
