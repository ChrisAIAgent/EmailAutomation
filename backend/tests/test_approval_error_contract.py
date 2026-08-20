import pytest

from app.services import approvals as approval_svc


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ({"ok": False, "blocked": "recipient_suppressed", "error": "transport_error"}, "recipient_suppressed"),
        ({"ok": False, "error": "transport_error"}, "transport_error"),
        ({"ok": False}, "draft_creation_failed"),
    ],
)
def test_require_draft_preserves_failure_reason_precedence(result, reason):
    with pytest.raises(approval_svc.DraftCreationError, match=reason):
        approval_svc._require_draft(result)
