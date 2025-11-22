"""
Unit tests for Anti-Fraud module.
Validates risk rules and scoring logic using data-driven tests.
"""
import pytest
from app.antifraude.schemas import AntifraudTransaction
from app.antifraude.rules import (
    AntifraudEngine,
    NightTimeRule,
    HighValueRule,
    ExcessiveAttemptsRule
)


@pytest.mark.parametrize("value, time, attempts, expected_approved, expected_risk", [
    (50.0, "14:30", 1, True, "LOW"),
    (350.0, "14:00", 1, True, "MEDIUM"),  # High value only (+30)
    (1500.0, "23:00", 5, False, "HIGH"),  # All rules (+30+40+50 = 120 -> 100)
])
def test_risk_analysis_scenarios(value: float, time: str, attempts: int, expected_approved: bool, expected_risk: str):
    """
    Data-driven test for risk analysis scenarios.
    Covers Low, Medium, and High risk cases.
    """
    engine = AntifraudEngine()
    transaction = AntifraudTransaction(
        value=value,
        time=time,
        attempts_last_24h=attempts,
        origin=None
    )

    result = engine.analyze(transaction)

    assert result["approved"] is expected_approved
    assert result["risk_level"] == expected_risk


@pytest.mark.parametrize("time, expected", [
    ("23:30", True),  # Night
    ("04:00", True),  # Early morning
    ("14:00", False),  # Afternoon
    ("06:01", False),  # Edge case
])
def test_night_time_rule(time: str, expected: bool):
    """Validates Night Time Rule boundary conditions."""
    rule = NightTimeRule()
    transaction = AntifraudTransaction(
        value=100.0,
        time=time,
        attempts_last_24h=1,
        origin=None
    )
    assert rule.evaluate(transaction) is expected


def test_high_value_rule():
    """Validates High Value Rule activation."""
    rule = HighValueRule(limit=300.0)

    # Value above limit
    high_tx = AntifraudTransaction(
        value=500.0,
        time="10:00",
        attempts_last_24h=1,
        origin=None
    )
    assert rule.evaluate(high_tx) is True

    # Value below limit
    low_tx = AntifraudTransaction(
        value=200.0,
        time="10:00",
        attempts_last_24h=1,
        origin=None
    )
    assert rule.evaluate(low_tx) is False


def test_excessive_attempts_rule():
    """Validates Excessive Attempts Rule activation."""
    rule = ExcessiveAttemptsRule(limit=3)

    # Excessive attempts
    excessive_tx = AntifraudTransaction(
        value=100.0,
        time="10:00",
        attempts_last_24h=5,
        origin=None
    )
    assert rule.evaluate(excessive_tx) is True

    # Normal attempts
    normal_tx = AntifraudTransaction(
        value=100.0,
        time="10:00",
        attempts_last_24h=2,
        origin=None
    )
    assert rule.evaluate(normal_tx) is False


def test_accumulated_score():
    """Verifies correct score accumulation from multiple rules."""
    engine = AntifraudEngine()

    transaction = AntifraudTransaction(
        value=400.0,  # +30 points
        time="01:00",  # +40 points
        attempts_last_24h=1,
        origin=None
    )

    result = engine.analyze(transaction)

    # Score should be 70 (30 + 40)
    assert result["score"] == 70
    assert len(result["triggered_rules"]) == 2


def test_invalid_time_validation():
    """Validates rejection of invalid time format."""
    with pytest.raises(Exception):
        AntifraudTransaction(
            value=100.0,
            time="25:00",  # Invalid time
            attempts_last_24h=1,
            origin=None
        )


def test_multiple_rules_activated():
    """Verifies simultaneous activation of multiple rules."""
    engine = AntifraudEngine()

    transaction = AntifraudTransaction(
        value=350.0,  # Activates high value rule
        time="23:00",  # Activates night time rule
        attempts_last_24h=4,  # Activates excessive attempts rule
        origin=None
    )

    result = engine.analyze(transaction)

    assert len(result["triggered_rules"]) == 3
    assert result["score"] == 100  # Capped at 100
