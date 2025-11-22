"""
Unit tests for Installment module.
Validates compound interest calculation, CET, and persistence.
"""
import pytest
from app.parcelamento.service import calculate_installments
from app.parcelamento.schemas import SimulationRequest


def test_basic_installment_calculation():
    """Tests basic installment calculation."""
    data = SimulationRequest(
        value=1000.0,
        installments=12,
        monthly_rate=0.035
    )

    result = calculate_installments(data)

    assert result["installment"] > 0
    assert result["total_paid"] > 1000.0
    assert result["annual_cet"] > 0
    assert len(result["table"]) == 12


def test_first_installment_interest():
    """Validates interest calculation for the first installment."""
    data = SimulationRequest(
        value=1000.0,
        installments=12,
        monthly_rate=0.035
    )

    result = calculate_installments(data)
    first = result["table"][0]

    # Interest of first installment = initial balance * rate
    assert abs(first["interest"] - 35.0) < 0.01
    assert first["month"] == 1


def test_final_balance_zero():
    """Verifies that the final balance is zero after all installments."""
    data = SimulationRequest(
        value=5000.0,
        installments=24,
        monthly_rate=0.02
    )

    result = calculate_installments(data)
    last = result["table"][-1]

    assert last["balance"] == 0.0


def test_total_paid_greater_than_value():
    """Total paid must be greater than principal (due to interest)."""
    data = SimulationRequest(
        value=2000.0,
        installments=10,
        monthly_rate=0.05
    )

    result = calculate_installments(data)

    assert result["total_paid"] > 2000.0


def test_negative_value_validation():
    """Validates rejection of negative value."""
    with pytest.raises(Exception):
        SimulationRequest(
            value=-100.0,
            installments=12,
            monthly_rate=0.035
        )


def test_excessive_rate_validation():
    """Validates rejection of excessive interest rate."""
    with pytest.raises(Exception):
        SimulationRequest(
            value=1000.0,
            installments=12,
            monthly_rate=0.20  # 20% - above limit
        )


def test_decreasing_balance():
    """Verifies that installments have decreasing balance."""
    data = SimulationRequest(
        value=3000.0,
        installments=6,
        monthly_rate=0.03
    )

    result = calculate_installments(data)

    for i in range(len(result["table"]) - 1):
        assert result["table"][i]["balance"] > result["table"][i + 1]["balance"]
