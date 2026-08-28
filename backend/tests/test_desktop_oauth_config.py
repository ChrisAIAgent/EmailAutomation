import pytest

from app import oauth_config


def valid_config():
    return {
        "installed": {
            "client_id": "1234567890-example.apps.googleusercontent.com",
            "client_secret": "GOCSPX-example-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def test_accepts_customer_desktop_oauth_shape():
    result = oauth_config.validate_desktop_config(valid_config())
    assert result["client_id"].endswith(".apps.googleusercontent.com")
    assert result["token_uri"] == "https://oauth2.googleapis.com/token"


def test_rejects_web_oauth_shape():
    payload = {"web": valid_config()["installed"]}
    with pytest.raises(ValueError, match="desktop_oauth_required"):
        oauth_config.validate_desktop_config(payload)


def test_rejects_untrusted_oauth_endpoints():
    payload = valid_config()
    payload["installed"]["token_uri"] = "https://example.invalid/token"
    with pytest.raises(ValueError, match="desktop_oauth_endpoint_invalid"):
        oauth_config.validate_desktop_config(payload)


def test_loopback_redirect_uses_runtime_backend_port():
    assert oauth_config.redirect_uri("http://127.0.0.1:18420") == "http://127.0.0.1:18420/api/gmail/oauth/callback"