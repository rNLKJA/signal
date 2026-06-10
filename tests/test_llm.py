"""LLM narrative path, tested with a fake anthropic module (no key, no network).

The contract under test:
  - when the LLM runs, the audit entry records the LLM model and provider
  - the prompt contains only computed aggregates, never raw records
  - any client failure falls back to the deterministic template, and the
    audit entry records the deterministic model — the log never lies about
    which model produced the words
"""

import sys
import types

import pytest

from signalkit.analyst.core import DETERMINISTIC_MODEL, Analyst, AnalystQuery

LLM_MODEL = "claude-haiku-4-5-20251001"
FAKE_NARRATIVE = "Narrative phrased by the fake LLM."


class FakeMessages:
    def __init__(self, fail: bool):
        self.fail = fail
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.fail:
            raise RuntimeError("simulated API failure")
        block = types.SimpleNamespace(text=FAKE_NARRATIVE)
        return types.SimpleNamespace(content=[block])


def install_fake_anthropic(monkeypatch, fail: bool) -> FakeMessages:
    messages = FakeMessages(fail)

    class FakeAnthropic:
        def __init__(self):
            self.messages = messages

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SIGNAL_LLM_MODEL", LLM_MODEL)
    return messages


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


def test_llm_narrative_recorded_in_audit(analyst, monkeypatch):
    messages = install_fake_anthropic(monkeypatch, fail=False)
    answer = analyst.ask(AnalystQuery(question="trend?", offense="burglary"))

    assert answer.narrative == FAKE_NARRATIVE
    assert answer.model_used == LLM_MODEL
    logged = analyst.recent_decisions()[-1]
    assert logged.model_name == LLM_MODEL
    assert logged.model_provider == "Anthropic"
    assert messages.last_kwargs["model"] == LLM_MODEL


def test_llm_prompt_contains_aggregates_only(analyst, monkeypatch):
    messages = install_fake_anthropic(monkeypatch, fail=False)
    analyst.ask(AnalystQuery(offense="burglary", borough="brooklyn"))

    prompt = messages.last_kwargs["messages"][0]["content"]
    assert "total_complaints" in prompt  # the computed stats are present
    assert "monthly_counts" in prompt
    # raw record fields must never appear: records carry per-row law_category
    assert "law_category" not in prompt


def test_llm_failure_falls_back_honestly(analyst, monkeypatch):
    install_fake_anthropic(monkeypatch, fail=True)
    answer = analyst.ask(AnalystQuery(offense="burglary"))

    assert answer.model_used == DETERMINISTIC_MODEL
    assert "NYPD recorded" in answer.narrative  # template narrative
    logged = analyst.recent_decisions()[-1]
    assert logged.model_name == DETERMINISTIC_MODEL
    assert logged.model_provider is None


def test_no_key_means_no_llm(analyst, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    answer = analyst.ask(AnalystQuery(offense="burglary"))
    assert answer.model_used == DETERMINISTIC_MODEL
