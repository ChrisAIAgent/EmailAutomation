"""Application exceptions."""
from __future__ import annotations


class AgentUnavailableError(Exception):
    def __init__(self, agent: str):
        self.agent = agent
        super().__init__(f"Agent '{agent}' is unavailable or not configured.")


class SafetyBlockedError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Blocked by policy: {reason}")
