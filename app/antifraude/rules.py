"""
Anti-Fraud Rule Engine.
Implements a configurable risk scoring system based on heuristic analysis.
"""
from typing import List, Dict, Any
from app.antifraude.schemas import TransacaoAntifraude
from app.core.logger import logger


class RegraAntifraude:
    """Abstract base class for fraud detection rules. Enforces the Strategy Pattern."""

    def __init__(self, nome: str, pontos: int, descricao: str):
        self.nome = nome
        self.pontos = pontos
        self.descricao = descricao

    def avaliar(self, transacao: TransacaoAntifraude) -> bool:
        """Evaluates the rule against the transaction context. Returns True if triggered."""
        raise NotImplementedError


class RegraHorarioNoturno(RegraAntifraude):
    """Heuristic: High-risk time window (22:00 - 06:00)."""

    def __init__(self):
        super().__init__(
            nome="HORARIO_NOTURNO",
            pontos=40,
            descricao="Transaction performed during high-risk hours (22h-6h)"
        )

    def avaliar(self, transacao: TransacaoAntifraude) -> bool:
        hora = int(transacao.horario.split(':')[0])
        # Night time: 22:00 inclusive to 06:00 exclusive
        return hora >= 22 or hora < 6


class RegraValorAlto(RegraAntifraude):
    """Heuristic: Transaction value exceeds standard threshold."""

    def __init__(self, limite: float = 300.0):
        super().__init__(
            nome="VALOR_ALTO",
            pontos=30,
            descricao=f"Transaction value exceeds R$ {limite}"
        )
        self.limite = limite

    def avaliar(self, transacao: TransacaoAntifraude) -> bool:
        return transacao.valor > self.limite


class RegraTentativasExcessivas(RegraAntifraude):
    """Heuristic: Velocity check (excessive attempts in 24h window)."""

    def __init__(self, limite: int = 3):
        super().__init__(
            nome="TENTATIVAS_EXCESSIVAS",
            pontos=50,
            descricao=f"More than {limite} attempts in the last 24h"
        )
        self.limite = limite

    def avaliar(self, transacao: TransacaoAntifraude) -> bool:
        return transacao.tentativas_ultimas_24h > self.limite


class RegraValorMuitoAlto(RegraAntifraude):
    """Heuristic: Extreme value anomaly detection."""

    def __init__(self, limite: float = 1000.0):
        super().__init__(
            nome="VALOR_EXTREMO",
            pontos=60,
            descricao=f"Transaction value exceeds R$ {limite} (extreme)"
        )
        self.limite = limite

    def avaliar(self, transacao: TransacaoAntifraude) -> bool:
        return transacao.valor > self.limite


class MotorAntifraude:
    """
    Fraud Detection Engine.
    Aggregates risk scores from registered rules and determines transaction approval status.
    Score < 60: Approved
    Score >= 60: Rejected
    """

    def __init__(self):
        self.regras: List[RegraAntifraude] = [
            RegraHorarioNoturno(),
            RegraValorAlto(limite=300.0),
            RegraTentativasExcessivas(limite=3),
            RegraValorMuitoAlto(limite=1000.0)
        ]
        self.limite_aprovacao = 60

    def analisar(self, transacao: TransacaoAntifraude) -> Dict[str, Any]:
        """
        Executes the rule chain against the transaction context.
        Returns a comprehensive risk assessment including score, decision, and triggered rules.
        """
        score = 0
        regras_ativadas: List[str] = []

        # Evaluate each rule
        for regra in self.regras:
            if regra.avaliar(transacao):
                score += regra.pontos
                regras_ativadas.append(f"{regra.nome}: {regra.descricao}")
                logger.info(f"Rule triggered: {regra.nome} (+{regra.pontos} points)")

        # Cap score at 100
        score = min(score, 100)

        # Determine approval status
        aprovado = score < self.limite_aprovacao

        # Determine risk level
        if score < 30:
            nivel_risco = "LOW"
            recomendacao = "Approve transaction"
        elif score < 60:
            nivel_risco = "MEDIUM"
            recomendacao = "Approve with monitoring"
        else:
            nivel_risco = "HIGH"
            recomendacao = "Reject and notify user"

        # Define reason
        if aprovado:
            motivo = "Transaction approved - acceptable risk"
        else:
            motivo = f"Transaction rejected - {nivel_risco.lower()} risk detected"

        resultado: Dict[str, Any] = {
            "score": score,
            "aprovado": aprovado,
            "motivo": motivo,
            "regras_ativadas": regras_ativadas if regras_ativadas else ["No rules triggered"],
            "nivel_risco": nivel_risco,
            "recomendacao": recomendacao
        }

        logger.info(f"Anti-fraud analysis completed: score={score}, approved={aprovado}, level={nivel_risco}")

        return resultado


# Singleton engine instance
motor_antifraude = MotorAntifraude()
