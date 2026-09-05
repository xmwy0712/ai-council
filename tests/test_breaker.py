"""Circuit breaker unit tests."""

from __future__ import annotations

import pytest

from council.core.breaker import CircuitBreaker


def test_trips_at_threshold() -> None:
    breaker = CircuitBreaker(threshold=3)
    assert breaker.failure("n1") is False
    assert breaker.failure("n1") is False
    assert breaker.failure("n1") is True  # exactly the tripping failure
    assert breaker.is_open("n1")


def test_stays_open_until_reset() -> None:
    breaker = CircuitBreaker(threshold=2)
    breaker.failure("n1")
    breaker.failure("n1")
    assert breaker.is_open("n1")
    breaker.failure("n1")  # extra failures do not re-trip
    assert breaker.is_open("n1")
    breaker.reset("n1")
    assert not breaker.is_open("n1")


def test_success_resets_the_streak() -> None:
    breaker = CircuitBreaker(threshold=3)
    breaker.failure("n1")
    breaker.failure("n1")
    breaker.success("n1")
    assert not breaker.is_open("n1")
    assert breaker.failure("n1") is False  # streak restarted, not continued
    assert breaker.failure("n1") is False
    assert breaker.failure("n1") is True


def test_nodes_are_independent() -> None:
    breaker = CircuitBreaker(threshold=1)
    breaker.failure("n1")
    assert breaker.is_open("n1")
    assert not breaker.is_open("n2")


def test_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(threshold=0)
