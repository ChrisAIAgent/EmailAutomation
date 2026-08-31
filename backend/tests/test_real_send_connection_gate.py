from types import SimpleNamespace

from app.config import Settings
from app.services.real_send import is_real_send_enabled


def _settings():
    return Settings(
        GOOGLE_CLIENT_ID="1234567890-example.apps.googleusercontent.com",
        GOOGLE_CLIENT_SECRET="customer-secret",
    )


def test_legacy_real_send_override_remains_available_for_internal_transport_tests():
    settings = _settings()
    settings.ENABLE_REAL_SEND = True
    account = SimpleNamespace(is_connected=False)
    oauth = SimpleNamespace(access_token_enc="encrypted-token")

    assert is_real_send_enabled(settings, account, oauth) is True


def test_real_send_is_enabled_by_verified_gmail_connection_without_env_toggle():
    account = SimpleNamespace(is_connected=True)
    oauth = SimpleNamespace(access_token_enc="encrypted-token")

    assert is_real_send_enabled(_settings(), account, oauth) is True


def test_real_send_is_disabled_when_oauth_token_is_missing():
    account = SimpleNamespace(is_connected=True)
    oauth = SimpleNamespace(access_token_enc="")

    assert is_real_send_enabled(_settings(), account, oauth) is False
