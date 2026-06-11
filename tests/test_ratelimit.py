"""Rate limiter: sliding-window behaviour (fake clock) and the 429 surface."""

import pytest
from fastapi.testclient import TestClient

from signalkit.analyst.core import Analyst
from signalkit.api import create_app
from signalkit.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_allows_under_limit():
    limiter = RateLimiter(limit=3, window_seconds=60, now=FakeClock())
    assert [limiter.check("a") for _ in range(3)] == [None, None, None]


def test_blocks_at_limit_with_retry_seconds():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, now=clock)
    limiter.check("a")
    clock.t += 10
    limiter.check("a")
    retry = limiter.check("a")
    assert retry == pytest.approx(50, abs=0.2)  # first hit frees up in 50s


def test_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, now=clock)
    assert limiter.check("a") is None
    assert limiter.check("a") is not None
    clock.t += 61
    assert limiter.check("a") is None


def test_keys_are_independent():
    limiter = RateLimiter(limit=1, window_seconds=60, now=FakeClock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None


def test_zero_limit_disables():
    limiter = RateLimiter(limit=0, window_seconds=60, now=FakeClock())
    assert all(limiter.check("a") is None for _ in range(100))


@pytest.fixture()
def limited_client(tmp_path):
    analyst = Analyst(log_path=str(tmp_path / "d.jsonl"), offline=True)
    return TestClient(create_app(analyst, rate_limiter=RateLimiter(limit=2, window_seconds=60)))


def test_ask_429_after_limit(limited_client):
    payload = {"offense": "burglary"}
    assert limited_client.post("/ask", json=payload).status_code == 200
    assert limited_client.post("/ask", json=payload).status_code == 200
    blocked = limited_client.post("/ask", json=payload)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert "Try again" in blocked.json()["detail"]


def test_only_ask_is_limited(limited_client):
    payload = {"offense": "burglary"}
    limited_client.post("/ask", json=payload)
    limited_client.post("/ask", json=payload)
    assert limited_client.post("/ask", json=payload).status_code == 429
    # read endpoints stay open
    assert limited_client.get("/decisions").status_code == 200
    assert limited_client.get("/governance/summary").status_code == 200
    assert limited_client.get("/health").status_code == 200
