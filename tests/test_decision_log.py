"""Governance log: schema validation and JSONL roundtrip."""

import pytest
from pydantic import ValidationError

from signalkit.governance.decision_log import DecisionEntry, DecisionLogger


def make_entry(**overrides):
    base = dict(
        model_name="signal-stats-v1 (deterministic)",
        input_summary="trend query",
        model_output_summary="complaints flat over window",
        decision_made="returned analysis",
        human_review_required=False,
    )
    base.update(overrides)
    return DecisionEntry(**base)


def test_entry_gets_id_and_timestamp():
    entry = make_entry()
    assert entry.decision_id.startswith("d-")
    assert entry.timestamp.tzinfo is not None


def test_override_requires_reason():
    with pytest.raises(ValidationError):
        make_entry(override_applied=True, override_reason=None)


def test_override_with_reason_is_valid():
    entry = make_entry(override_applied=True, override_reason="analyst disagreed with model")
    assert entry.override_reason


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        make_entry(confidence_score=1.5)


def test_logger_roundtrip(tmp_path):
    log_path = tmp_path / "decisions.jsonl"
    logger = DecisionLogger(log_path)
    first = make_entry()
    second = make_entry(human_review_required=True)
    logger.log(first)
    logger.log(second)

    entries = logger.read_all()
    assert [e.decision_id for e in entries] == [first.decision_id, second.decision_id]
    assert entries[1].human_review_required is True


def test_read_all_on_missing_file(tmp_path):
    logger = DecisionLogger(tmp_path / "never_written.jsonl")
    assert logger.read_all() == []


def test_summarise_aggregates():
    from signalkit.governance.decision_log import summarise

    entries = [
        make_entry(),
        make_entry(human_review_required=True, model_name="claude-haiku-4-5-20251001"),
        make_entry(human_review_required=True),
    ]
    summary = summarise(entries)
    assert summary.total_decisions == 3
    assert summary.human_review_required_count == 2
    assert summary.human_review_rate == round(2 / 3, 3)
    assert summary.by_risk_category == {"limited": 3}
    assert summary.by_model["claude-haiku-4-5-20251001"] == 1
    assert summary.first_decision_at <= summary.last_decision_at


def test_summarise_empty():
    from signalkit.governance.decision_log import summarise

    summary = summarise([])
    assert summary.total_decisions == 0
    assert summary.human_review_rate is None
    assert summary.first_decision_at is None
