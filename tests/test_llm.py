"""LLM narrative path, tested against a faked OpenAI-compatible endpoint
(no key, no network, no provider SDK).

The contract under test:
  - when the LLM runs, the audit entry records the configured model and
    provider — the log never lies about which model produced the words
  - the request goes to {base_url}/chat/completions with a bearer key
  - the prompt contains only computed aggregates, never raw records
  - any client failure falls back to the deterministic template, and the
    audit entry records the deterministic model
"""

import types

import httpx
import pytest

from signalkit.analyst.core import DETERMINISTIC_MODEL, Analyst, AnalystQuery

LLM_MODEL = "deepseek-chat"
FAKE_NARRATIVE = "Narrative phrased by the fake LLM."


class FakePost:
    def __init__(self, fail: bool, content: str = FAKE_NARRATIVE):
        self.fail = fail
        self.content = content
        self.calls = 0
        self.last_url = None
        self.last_kwargs = None

    def __call__(self, url, **kwargs):
        self.calls += 1
        self.last_url = url
        self.last_kwargs = kwargs
        if self.fail:
            raise httpx.ConnectError("simulated API failure")
        payload = {"choices": [{"message": {"content": self.content}}]}
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def install_fake_llm(monkeypatch, fail: bool, content: str = FAKE_NARRATIVE) -> FakePost:
    fake = FakePost(fail, content)
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setenv("SIGNAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("SIGNAL_LLM_MODEL", LLM_MODEL)
    monkeypatch.setenv("SIGNAL_LLM_PROVIDER", "DeepSeek")
    return fake


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


def test_llm_narrative_recorded_in_audit(analyst, monkeypatch):
    fake = install_fake_llm(monkeypatch, fail=False)
    answer = analyst.ask(AnalystQuery(question="trend?", offense="theft"))

    assert answer.narrative == FAKE_NARRATIVE
    assert answer.model_used == LLM_MODEL
    logged = analyst.recent_decisions()[-1]
    assert logged.model_name == LLM_MODEL
    assert logged.model_provider == "DeepSeek"
    assert fake.last_kwargs["json"]["model"] == LLM_MODEL


def test_llm_request_shape(analyst, monkeypatch):
    fake = install_fake_llm(monkeypatch, fail=False)
    analyst.ask(AnalystQuery(offense="theft"))

    assert fake.last_url == "https://api.deepseek.com/chat/completions"
    assert fake.last_kwargs["headers"]["Authorization"] == "Bearer test-key"


def test_llm_base_url_configurable(analyst, monkeypatch):
    fake = install_fake_llm(monkeypatch, fail=False)
    monkeypatch.setenv("SIGNAL_LLM_BASE_URL", "https://example.com/v1/")
    analyst.ask(AnalystQuery(offense="theft"))

    assert fake.last_url == "https://example.com/v1/chat/completions"


def test_llm_prompt_contains_aggregates_only(analyst, monkeypatch):
    fake = install_fake_llm(monkeypatch, fail=False)
    analyst.ask(AnalystQuery(offense="theft", region="adelaide"))

    prompt = fake.last_kwargs["json"]["messages"][0]["content"]
    assert "total_offences" in prompt  # the computed stats are present
    assert "monthly_counts" in prompt
    # Leak canary: the query is filtered to Adelaide, so a different region
    # appearing in the prompt would mean raw records leaked through.
    assert "ELIZABETH" not in prompt
    assert "WHYALLA" not in prompt


def test_llm_failure_falls_back_honestly(analyst, monkeypatch):
    install_fake_llm(monkeypatch, fail=True)
    answer = analyst.ask(AnalystQuery(offense="theft"))

    assert answer.model_used == DETERMINISTIC_MODEL
    assert "SA Police recorded" in answer.narrative  # template narrative
    logged = analyst.recent_decisions()[-1]
    assert logged.model_name == DETERMINISTIC_MODEL
    assert logged.model_provider is None


def test_empty_llm_content_falls_back(analyst, monkeypatch):
    """A reasoning model that burns its budget returns empty content —
    that must never reach a user as a blank narrative."""
    install_fake_llm(monkeypatch, fail=False, content="")
    answer = analyst.ask(AnalystQuery(offense="theft"))

    assert answer.model_used == DETERMINISTIC_MODEL
    assert "SA Police recorded" in answer.narrative


def test_no_key_means_no_llm(analyst):
    answer = analyst.ask(AnalystQuery(offense="theft"))
    assert answer.model_used == DETERMINISTIC_MODEL


def test_identical_queries_hit_the_cache(analyst, monkeypatch):
    """Same aggregates → one upstream call, but both decisions audit-logged
    with the (honest) LLM attribution."""
    fake = install_fake_llm(monkeypatch, fail=False)
    first = analyst.ask(AnalystQuery(offense="theft"))
    second = analyst.ask(AnalystQuery(offense="theft"))

    assert fake.calls == 1  # second narrative came from the cache
    assert first.narrative == second.narrative
    assert first.decision_id != second.decision_id  # still two audit entries
    logged = analyst.recent_decisions()
    assert all(e.model_name == LLM_MODEL for e in logged[-2:])


def test_different_queries_miss_the_cache(analyst, monkeypatch):
    fake = install_fake_llm(monkeypatch, fail=False)
    analyst.ask(AnalystQuery(offense="theft"))
    analyst.ask(AnalystQuery(offense="robbery"))
    assert fake.calls == 2
