"""Tests for measuring the faithfulness check itself (v2 workstream 3).

The point is honest self-measurement: the deterministic check is conservative
(it never wrongly rejects a faithful narrative) but blind to semantic errors, and
the LLM judge is the second signal that covers them.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from signalkit.analyst import eval as ev
from signalkit.analyst.core import Analyst, _llm_judge
from signalkit.api import create_app


@pytest.fixture()
def client(tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    return TestClient(create_app(analyst))


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content

    def __call__(self, url, **kwargs):
        payload = {"choices": [{"message": {"content": self.content}}]}
        import types
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


# --- the deterministic check, measured --------------------------------------


def test_deterministic_check_is_measured_honestly():
    d = ev.measure_check()["deterministic_check"]
    assert d["precision"] == 1.0          # never wrongly rejects a faithful narrative
    assert d["confusion"]["fp"] == 0
    assert 0.3 < d["recall"] < 0.7        # catches mechanical errors, misses semantic ones
    # the blind spots are reported, not hidden
    assert {"sem-1", "claim-1", "tol-1"} <= set(d["missed_case_ids"])
    assert d["by_category"]["semantic-mislabel"]["det_correct"] == 0


def test_judge_as_second_signal_lifts_recall_and_tracks_agreement():
    truth = {c["narrative"]: c["label"] == "faithful" for c in ev.EVAL_SET}

    def oracle(narrative, allowed, trend):  # an ideal judge that knows the labels
        return truth[narrative]

    j = ev.measure_check(judge_fn=oracle)["llm_judge"]
    assert j["recall"] == 1.0  # the second signal catches what the first missed
    assert j["precision"] == 1.0
    assert 0.0 <= j["agreement_with_deterministic"] <= 1.0


def test_judge_unavailable_without_key():
    assert ev.measure_check()["llm_judge"]["available"] is False


# --- exposed live -----------------------------------------------------------


def test_faithfulness_eval_endpoint(client):
    body = client.get("/governance/faithfulness-eval").json()
    assert body["labelled_cases"] == len(ev.EVAL_SET)
    assert body["deterministic_check"]["precision"] == 1.0


def test_model_card_includes_check_validation(client):
    card = client.get("/governance/model-card").json()
    assert card["check_validation"]["deterministic_check"]["recall"] is not None
    assert "How good is the faithfulness check" in card["card"]


# --- the LLM judge itself ---------------------------------------------------


def test_llm_judge_parses_both_verdicts(monkeypatch):
    monkeypatch.setenv("SIGNAL_LLM_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "post", _FakeLLM("UNFAITHFUL"))
    assert _llm_judge("alpha narrative", {1.0, 2.0}, "falling") is False
    monkeypatch.setattr(httpx, "post", _FakeLLM("FAITHFUL"))
    assert _llm_judge("beta narrative", {1.0, 2.0}, "falling") is True


def test_llm_judge_is_none_without_key(monkeypatch):
    monkeypatch.delenv("SIGNAL_LLM_API_KEY", raising=False)
    assert _llm_judge("gamma", {1.0}, "falling") is None
