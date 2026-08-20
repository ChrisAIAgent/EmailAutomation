"""Structured API error contract for machine-readable Agent errors.

Endpoints raise ``ApiError`` instead of a bare ``HTTPException`` so the response
body always carries a stable ``code`` the Agent can branch on, e.g.
``GMAIL_SYNC_FAILED`` or ``APPROVAL_BLOCKED``. This replaces the previous
free-text ``detail`` strings that forced the Agent to string-match messages.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    """Structured error with a stable ``code`` the Agent can branch on.

    Subclasses ``HTTPException`` so existing ``pytest.raises(HTTPException)``
    and FastAPI's default handling still apply; the registered handler below
    overrides the default ``{"detail": ...}`` body with a structured contract.
    """

    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.details = details


# Human-readable descriptions for the documented codes (see docs/AGENT_OPERATIONS.md).
ERROR_CODES = {
    "GMAIL_SYNC_FAILED": "Gmail inbox sync did not complete.",
    "APPROVAL_BLOCKED": "Approval could not be dispatched (policy / send gate).",
    "DRAFT_ONLY_MODE": "Real sending is disabled (ENABLE_REAL_SEND=false).",
    "NO_REAL_GMAIL": "Real send enabled but no real Gmail account is connected.",
    "POLICY_BLOCKED": "Blocked by the policy engine (suppression / window / limit).",
}


def register_error_handlers(app):
    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )
