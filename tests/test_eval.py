"""Narrative faithfulness eval: deterministic checks, the analyst gate that
rejects unfaithful LLM output, and the live model card.

The gate is the governance point: an LLM narrative that states a number not in
the computed statistics, or contradicts the trend, never reaches a user — the
deterministic template is served and the rejection is logged.
"""

import types

import httpx
import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import DETERMINISTIC_MODEL, Analyst, AnalystQuery, TrendStats
from signalkit.analyst.eval import allowed_from_stats, evaluate
from signalkit.api import create_app


def _stats() -> TrendStats:
    return TrendStats(
        window_start="2025-04",
        window_end="2026-03",
        total_offences=829,
        monthly_counts={"2025-04": 70, "2026-03": 80},
        mom_change_pct=6.5,
        yoy_change_pct=-47.3,
        trend_direction="falling",
        anomalous_months=[],
        top_offenses=[{"offense": "Theft", "count": 829}],
        by_offense_division={"Offences against property": 829},
    )


# --- unit: the deterministic checker ---

def test_faithful_narrative_passes():
    s = _stats()
    text = (
        "Between 2025-04 and 2026-03, SA Police recorded 829 offences. "
        "The trend over the window is falling. The latest month is up 6.5% on the "
        "month before. Year on year, the latest month is down 47.3%."
    )
    report = evaluate(text, allowed_from_stats(s), trend_direction=s.trend_direction)
    assert report.passed
    assert report.score == 1.0
    assert report.issues == []


def test_fabricated_figure_is_caught():
    s = _stats()
    report = evaluate(
        "SA Police recorded 1,234 offences over the window.",
        allowed_from_stats(s),
        trend_direction=s.trend_direction,
    )
    assert not report.passed
    assert any("1234" in issue for issue in report.issues)


def test_trend_contradiction_is_caught():
    s = _stats()  # computed as falling
    report = evaluate(
        "There were 829 offences. The trend over the window is rising.",
        allowed_from_stats(s),
        trend_direction=s.trend_direction,
    )
    assert not report.passed
    assert any("trend" in issue for issue in report.issues)


def test_month_on_month_up_does_not_trip_a_falling_trend():
    """'up 6.5% on the month' is MoM phrasing, not a trend claim — must not flag."""
    s = _stats()
    report = evaluate(
        "The trend over the window is falling. The latest month is up 6.5% on the month before.",
        allowed_from_stats(s),
        trend_direction=s.trend_direction,
    )
    assert report.passed


def test_no_numbers_is_trivially_faithful():
    assert evaluate("A short qualitative note with no figures.", set()).passed


# --- integration: the analyst gate ---

class _FakeLLM:
    def __init__(self, content: str):
        self.content = content

    def __call__(self, url, **kwargs):
        payload = {"choices": [{"message": {"content": self.content}}]}
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def _use_llm(monkeypatch, content: str):
    monkeypatch.setattr(httpx, "post", _FakeLLM(content))
    monkeypatch.setenv("SIGNAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("SIGNAL_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("SIGNAL_LLM_PROVIDER", "DeepSeek")


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


def test_gate_rejects_hallucinated_number_and_logs_it(analyst, monkeypatch):
    _use_llm(monkeypatch, "SA Police recorded 999999 offences — a fabricated figure.")
    answer = analyst.ask(AnalystQuery(offense="theft"))

    assert answer.model_used == DETERMINISTIC_MODEL          # fell back to the template
    assert "999999" not in answer.narrative
    assert answer.faithfulness_score == 1.0                  # the served text is faithful

    logged = analyst.recent_decisions()[-1]
    assert "faithfulness-fallback" in logged.tags
    assert logged.notes and "faithfulness" in logged.notes.lower()


def test_gate_passes_a_faithful_llm_narrative(analyst, monkeypatch):
    _use_llm(monkeypatch, "Theft is broadly in line with the rest of the window.")
    answer = analyst.ask(AnalystQuery(offense="theft"))

    assert answer.model_used == "deepseek-chat"
    assert answer.faithfulness_score == 1.0
    logged = analyst.recent_decisions()[-1]
    assert "faithfulness-fallback" not in logged.tags


# --- model card ---

def test_model_card_endpoint_reports_live_eval(monkeypatch, tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    client = TestClient(create_app(analyst))

    # one fabricated-number answer → a fallback the card should count
    _use_llm(monkeypatch, "SA Police recorded 999999 offences.")
    client.post("/ask", json={"offense": "theft"})

    card = client.get("/governance/model-card").json()
    assert card["version"]
    assert card["narrative_eval"]["decisions_evaluated"] >= 1
    assert card["narrative_eval"]["fallbacks_to_template"] >= 1
    assert any("deterministic" in c["type"] for c in card["components"])
