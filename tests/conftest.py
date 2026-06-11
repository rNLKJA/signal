"""Shared test guards."""

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_llm_key(monkeypatch):
    """Tests must never pick up a real LLM key from the developer's shell.

    The LLM tests set SIGNAL_LLM_API_KEY explicitly after this runs.
    """
    monkeypatch.delenv("SIGNAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("SIGNAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SIGNAL_LLM_MODEL", raising=False)
    monkeypatch.delenv("SIGNAL_LLM_PROVIDER", raising=False)
