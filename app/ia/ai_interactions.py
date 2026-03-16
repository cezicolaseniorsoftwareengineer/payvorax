"""
AI Interaction log — immutable audit trail for every LLM call.
Records user_id, snapshot_hash, model used, response length.
Never stores question text, response text, or any PII.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column, Session
from app.core.database import Base
from app.core.logger import audit_log


class AiInteraction(Base):
    """Immutable record of every IA agent call. Never updated after insert."""

    __tablename__ = "ai_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    question_length: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_length: Mapped[int] = mapped_column(Integer, nullable=False)
    model_used: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )


def log_interaction(
    db: Session,
    user_id: str,
    question: str,
    snapshot_dict: dict,
    response: str,
    model: str,
) -> None:
    """
    Logs an AI interaction audit record.
    Hashes the snapshot for auditability without storing PII.
    Never blocks the chat response on failure.
    """
    try:
        snap_hash = hashlib.sha256(
            json.dumps(snapshot_dict, sort_keys=True, default=str).encode()
        ).hexdigest()

        record = AiInteraction(
            user_id=user_id,
            question_length=len(question),
            snapshot_hash=snap_hash,
            response_length=len(response),
            model_used=model,
        )
        db.add(record)
        db.commit()

        audit_log(
            action="AI_INTERACTION",
            user=user_id,
            resource="ia_chat",
            details={"snapshot_hash": snap_hash, "model": model},
        )
    except Exception:
        pass
