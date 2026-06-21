"""Tests for the tamper-evident audit-log hash chain.

The point of the chain is that altering, deleting or reordering a logged
decision is *detectable*. These tests prove the detection, not just the happy path.
"""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst
from signalkit.api import create_app
from signalkit.governance.decision_log import (
    DecisionEntry,
    DecisionLogger,
    verify_chain,
)


def _entry(decision: str) -> DecisionEntry:
    return DecisionEntry(
        model_name="signal-stats-v1",
        input_summary=f"input for {decision}",
        model_output_summary=f"output for {decision}",
        decision_made=decision,
        human_review_required=False,
    )


def _logger(tmp_path) -> DecisionLogger:
    return DecisionLogger(str(tmp_path / "decisions.jsonl"))


# --- chaining --------------------------------------------------------------


def test_log_chains_each_entry_to_the_previous(tmp_path):
    log = _logger(tmp_path)
    log.log(_entry("a"))
    log.log(_entry("b"))
    log.log(_entry("c"))
    entries = log.read_all()
    assert entries[0].prev_hash == DecisionLogger.GENESIS
    assert entries[0].entry_hash is not None
    # each entry points at its predecessor's hash
    assert entries[1].prev_hash == entries[0].entry_hash
    assert entries[2].prev_hash == entries[1].entry_hash


def test_clean_log_verifies(tmp_path):
    log = _logger(tmp_path)
    for d in ("a", "b", "c"):
        log.log(_entry(d))
    report = log.verify()
    assert report.valid is True
    assert report.chained_entries == 3
    assert report.legacy_entries == 0
    assert report.head_hash == log.read_all()[-1].entry_hash


def test_empty_log_verifies(tmp_path):
    assert _logger(tmp_path).verify().valid is True


# --- detection -------------------------------------------------------------


def test_editing_an_entry_is_detected(tmp_path):
    log = _logger(tmp_path)
    log.log(_entry("a"))
    log.log(_entry("b"))
    log.log(_entry("c"))
    target_id = log.read_all()[1].decision_id

    # Tamper: rewrite the middle line with an altered decision but the old hash.
    lines = log.path.read_text().splitlines()
    lines[1] = lines[1].replace('"decision_made":"b"', '"decision_made":"EDITED"')
    log.path.write_text("\n".join(lines) + "\n")

    report = verify_chain(log.read_all())
    assert report.valid is False
    assert report.broken_at == target_id
    assert "altered" in report.reason


def test_deleting_an_entry_is_detected(tmp_path):
    log = _logger(tmp_path)
    log.log(_entry("a"))
    log.log(_entry("b"))
    log.log(_entry("c"))
    broken_id = log.read_all()[2].decision_id

    # Tamper: remove the middle entry. Entry 'c' now follows 'a', so its
    # prev_hash no longer matches.
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")

    report = verify_chain(log.read_all())
    assert report.valid is False
    assert report.broken_at == broken_id
    assert "prev_hash" in report.reason


# --- backward compatibility with a pre-hashing log -------------------------


def test_legacy_entries_are_counted_not_failed(tmp_path):
    log = _logger(tmp_path)
    # Two legacy lines written without a hash (as the live log was before v2).
    with log.path.open("w", encoding="utf-8") as f:
        f.write(_entry("old-1").to_jsonl_line() + "\n")
        f.write(_entry("old-2").to_jsonl_line() + "\n")
    # A fresh logger picks up from the tail and chains new entries from genesis.
    log2 = DecisionLogger(str(log.path))
    log2.log(_entry("new-1"))
    log2.log(_entry("new-2"))

    report = log2.verify()
    assert report.valid is True
    assert report.legacy_entries == 2
    assert report.chained_entries == 2


def test_unchained_entry_after_chain_is_detected(tmp_path):
    log = _logger(tmp_path)
    log.log(_entry("a"))
    log.log(_entry("b"))
    # Tamper: append a legacy (unhashed) entry after the chain has begun.
    with log.path.open("a", encoding="utf-8") as f:
        f.write(_entry("injected").to_jsonl_line() + "\n")
    report = log.verify()
    assert report.valid is False


# --- API -------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "decisions.jsonl"), offline=True)
    return TestClient(create_app(analyst))


def test_verify_endpoint_reports_intact_chain(client):
    client.post("/ask", json={"offense": "theft"})
    client.post("/ask", json={"offense": "robbery"})
    body = client.get("/decisions/verify").json()
    assert body["valid"] is True
    assert body["chained_entries"] >= 2
    assert body["head_hash"]


def test_verify_route_not_shadowed_by_decision_id(client):
    # 'verify' must hit the verify endpoint, not be read as a decision_id 404.
    assert client.get("/decisions/verify").status_code == 200
