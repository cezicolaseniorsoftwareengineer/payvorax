"""
Anti-Fraud Rule Engine.
Implements a configurable risk scoring system based on heuristic analysis.
"""
from typing import List, Dict, Any
from app.antifraude.schemas import AntifraudTransaction
from app.core.logger import logger


class AntifraudRule:
    """Abstract base class for fraud detection rules. Enforces the Strategy Pattern."""

    def __init__(self, name: str, points: int, description: str):
        self.name = name
        self.points = points
        self.description = description

    def evaluate(self, transaction: AntifraudTransaction) -> bool:
        """Evaluates the rule against the transaction context. Returns True if triggered."""
        raise NotImplementedError


class NightTimeRule(AntifraudRule):
    """Heuristic: High-risk time window (22:00 - 06:00)."""

    def __init__(self):
        super().__init__(
            name="NIGHT_TIME",
            points=40,
            description="Transaction performed during high-risk hours (22h-6h)"
        )

    def evaluate(self, transaction: AntifraudTransaction) -> bool:
        hour = int(transaction.time.split(':')[0])
        # Night time: 22:00 inclusive to 06:00 exclusive
        return hour >= 22 or hour < 6


class HighValueRule(AntifraudRule):
    """Heuristic: Transaction value exceeds standard threshold."""

    def __init__(self, limit: float = 300.0):
        super().__init__(
            name="HIGH_VALUE",
            points=30,
            description=f"Transaction value exceeds R$ {limit}"
        )
        self.limit = limit

    def evaluate(self, transaction: AntifraudTransaction) -> bool:
        return transaction.value > self.limit


class ExcessiveAttemptsRule(AntifraudRule):
    """Heuristic: Velocity check (excessive attempts in 24h window)."""

    def __init__(self, limit: int = 3):
        super().__init__(
            name="EXCESSIVE_ATTEMPTS",
            points=50,
            description=f"More than {limit} attempts in the last 24h"
        )
        self.limit = limit

    def evaluate(self, transaction: AntifraudTransaction) -> bool:
        return transaction.attempts_last_24h > self.limit


class ExtremeValueRule(AntifraudRule):
    """Heuristic: Extreme value anomaly detection."""

    def __init__(self, limit: float = 1000.0):
        super().__init__(
            name="EXTREME_VALUE",
            points=60,
            description=f"Transaction value exceeds R$ {limit} (extreme)"
        )
        self.limit = limit

    def evaluate(self, transaction: AntifraudTransaction) -> bool:
        return transaction.value > self.limit


class AntifraudEngine:
    """
    Fraud Detection Engine.
    Aggregates risk scores from registered rules and determines transaction approval status.
    Score < 60: Approved
    Score >= 60: Rejected
    """

    def __init__(self):
        self.rules: List[AntifraudRule] = [
            NightTimeRule(),
            HighValueRule(limit=300.0),
            ExcessiveAttemptsRule(limit=3),
            ExtremeValueRule(limit=1000.0)
        ]
        self.approval_limit = 60

    def analyze(self, transaction: AntifraudTransaction) -> Dict[str, Any]:
        """
        Executes the rule chain against the transaction context.
        Returns a comprehensive risk assessment including score, decision, and triggered rules.
        """
        score = 0
        triggered_rules: List[str] = []

        # Evaluate each rule
        for rule in self.rules:
            if rule.evaluate(transaction):
                score += rule.points
                triggered_rules.append(f"{rule.name}: {rule.description}")
                logger.info(f"Rule triggered: {rule.name} (+{rule.points} points)")

        # Cap score at 100
        score = min(score, 100)

        # Determine approval status
        approved = score < self.approval_limit

        # Determine risk level
        if score < 30:
            risk_level = "LOW"
            recommendation = "Approve transaction"
        elif score < 60:
            risk_level = "MEDIUM"
            recommendation = "Approve with monitoring"
        else:
            risk_level = "HIGH"
            recommendation = "Reject and notify user"

        # Define reason
        if approved:
            reason = "Transaction approved - acceptable risk"
        else:
            reason = f"Transaction rejected - {risk_level.lower()} risk detected"

        result: Dict[str, Any] = {
            "score": score,
            "approved": approved,
            "reason": reason,
            "triggered_rules": triggered_rules if triggered_rules else ["No rules triggered"],
            "risk_level": risk_level,
            "recommendation": recommendation
        }

        logger.info(f"Anti-fraud analysis completed: score={score}, approved={approved}, level={risk_level}")

        return result


# Singleton engine instance
antifraud_engine = AntifraudEngine()
