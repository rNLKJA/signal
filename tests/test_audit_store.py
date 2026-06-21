"""Tests for the pluggable AuditStore abstraction (v3).

The point: the tamper-evidence and logging guarantees live in DecisionLogger and
hold over *any* store. These tests run the same guarantees through an in-memory
backend and a duck-typed custom store, proving a durable backend (Postgres, …)
could be dropped in without weakening anything.
"""

from signalkit.governance import (
    AuditStore,
    DecisionEntry,
    DecisionLogger,
    InMemoryAuditStore,
    JsonlAuditStore,
)


def _entry(decision: str) -> DecisionEntry:
    return DecisionEntry(
        model_name="m",
        input_summary=f"in {decision}",
        model_output_summary=f"out {decision}",
        decision_made=decision,
        human_review_required=False,
    )


# --- the stores themselves --------------------------------------------------


def test_inmemory_store_roundtrip():
    s = InMemoryAuditStore()
    assert s.read_lines() == [] and s.last_line() is None
    s.append("a")
    s.append("b")
    assert s.read_lines() == ["a", "b"]
    assert s.last_line() == "b"


def test_jsonl_store_roundtrip(tmp_path):
    s = JsonlAuditStore(str(tmp_path / "x.jsonl"))
    assert s.read_lines() == [] and s.last_line() is None
    s.append("a")
    s.append("b")
    assert s.read_lines() == ["a", "b"]
    assert s.last_line() == "b"


def test_stores_satisfy_the_protocol():
    assert isinstance(InMemoryAuditStore(), AuditStore)
    assert isinstance(JsonlAuditStore("/tmp/whatever.jsonl"), AuditStore)


# --- the guarantees hold over a non-file backend ----------------------------


def test_logger_works_over_an_in_memory_store():
    log = DecisionLogger(InMemoryAuditStore())
    for d in ("a", "b", "c"):
        log.log(_entry(d))
    entries = log.read_all()
    assert [e.decision_made for e in entries] == ["a", "b", "c"]
    # the chain is intact, exactly as with a file
    assert log.verify().valid
    assert entries[1].prev_hash == entries[0].entry_hash


def test_tamper_evidence_holds_over_an_in_memory_store():
    store = InMemoryAuditStore()
    log = DecisionLogger(store)
    log.log(_entry("a"))
    log.log(_entry("b"))
    # tamper with a stored line directly in the backend
    store._lines[0] = store._lines[0].replace('"decision_made":"a"', '"decision_made":"X"')
    report = log.verify()
    assert report.valid is False
    assert "altered" in report.reason


def test_logger_accepts_a_duck_typed_custom_store():
    # Any object with append/read_lines/last_line works — no inheritance needed.
    class ListStore:
        def __init__(self):
            self.rows = []

        def append(self, line):
            self.rows.append(line)

        def read_lines(self):
            return list(self.rows)

        def last_line(self):
            return self.rows[-1] if self.rows else None

    log = DecisionLogger(ListStore())
    log.log(_entry("a"))
    log.log(_entry("b"))
    assert log.verify().valid
    assert len(log.read_all()) == 2


# --- backward compatibility -------------------------------------------------


def test_path_constructor_still_uses_a_jsonl_store(tmp_path):
    p = tmp_path / "decisions.jsonl"
    log = DecisionLogger(str(p))
    log.log(_entry("a"))
    assert log.path == p  # .path preserved for file-backed logs
    assert p.exists()
    assert log.verify().valid
