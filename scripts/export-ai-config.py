"""Export the Email Automation LLM config to the trusted parent startup process.

Only Email Automation's own LLM (used by the LangGraph email agent) is exported.
TACWork's AI provider is configured entirely inside the TACWork web UI and is
never injected here.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(os.path.join(ROOT, "backend"))

from app.db import SessionLocal, init_db  # noqa: E402
from app.services.ai_config import load_email  # noqa: E402

init_db()
db = SessionLocal()
try:
    email = load_email(db)
    print(json.dumps({
        "LLM_PROVIDER": email.provider_name,
        "LLM_BASE_URL": email.base_url,
        "LLM_MODEL": email.model,
        "LLM_API_KEY": email.api_key,
    }))
finally:
    db.close()
