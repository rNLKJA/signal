"""Analyst layer: stats correctness, governance logging, error paths."""

import pytest

from signalkit.analyst.core import Analyst, AnalystQuery, NoDataError, compute_stats
from signalkit.data.sa_crime import MonthlyRecord


@pytest.fixture()
def analyst(tmp_path):
    return Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)


def test_ask_statewide(analyst):
    answer = analyst.ask(AnalystQuery(question="overall trend", months=12))
    assert answer.stats.total_offences > 0
    assert len(answer.stats.monthly_counts) == 12
    assert answer.narrative
    assert answer.decision_id.startswith("d-")
    assert "snapshot" in answer.data_source


def test_ask_filtered(analyst):
    answer = analyst.ask(AnalystQuery(offense="theft", region="adelaide", months=12))
    assert answer.stats.total_offences > 0
    # The only offence matching 'theft' is the harmonised 'Theft' category.
    assert answer.stats.top_offenses[0]["offense"] == "Theft"


def test_every_answer_is_logged(analyst):
    a1 = analyst.ask(AnalystQuery(offense="robbery"))
    a2 = analyst.ask(AnalystQuery(offense="assault"))
    logged = analyst.recent_decisions()
    assert [e.decision_id for e in logged] == [a1.decision_id, a2.decision_id]
    assert all(e.data_sources for e in logged)


def test_no_match_raises_with_suggestions(analyst):
    with pytest.raises(NoDataError) as exc:
        analyst.ask(AnalystQuery(offense="space piracy"))
    assert "regions" in exc.value.suggestions
    assert exc.value.suggestions["regions"]


def test_months_window_respected(analyst):
    answer = analyst.ask(AnalystQuery(months=6))
    assert len(answer.stats.monthly_counts) == 6


def _records(counts: list[int]) -> list[MonthlyRecord]:
    return [
        MonthlyRecord(
            month=f"2025-{i + 1:02d}",
            region="TESTVILLE",
            offense="Theft",
            offense_division="Offences against property",
            count=c,
        )
        for i, c in enumerate(counts)
    ]


def test_compute_stats_rising_trend():
    stats = compute_stats(_records([100, 110, 120, 130, 140, 150]), months=6)
    assert stats.trend_direction == "rising"
    assert stats.mom_change_pct == pytest.approx(7.1, abs=0.1)


def test_compute_stats_anomaly_flagged():
    stats = compute_stats(_records([100, 100, 100, 100, 100, 100, 100, 300]), months=8)
    assert "2025-08" in stats.anomalous_months


def test_anomaly_sets_human_review(tmp_path, monkeypatch):
    analyst = Analyst(log_path=str(tmp_path / "d.jsonl"), offline=True)
    monkeypatch.setattr(
        "signalkit.analyst.core.get_records",
        lambda offline=None: (_records([100, 100, 100, 100, 100, 100, 100, 300]), "test-data"),
    )
    answer = analyst.ask(AnalystQuery(months=8))
    assert answer.human_review_required is True
    assert analyst.recent_decisions()[-1].human_review_required is True
