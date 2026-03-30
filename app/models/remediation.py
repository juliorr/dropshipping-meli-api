"""Remediation models — error logging, rules, and fix attempts."""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PublishErrorLog(Base):
    """Logs every publish failure with full context for analysis."""

    __tablename__ = "publish_error_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    listing_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # ML context
    meli_category_id: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, index=True
    )

    # Error classification
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)

    # Full context (JSONB for querying)
    ml_response: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    publish_payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    # Remediation tracking
    remediation_attempted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    remediation_rule_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("remediation_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    remediation_succeeded: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PublishErrorLog(id={self.id}, error_code='{self.error_code}', "
            f"remediation_succeeded={self.remediation_succeeded})>"
        )


class RemediationRule(Base):
    """Pattern-based rules for auto-fixing publish errors."""

    __tablename__ = "remediation_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Matching criteria
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    cause_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_pattern: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    category_pattern: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Fix definition
    fix_type: Mapped[str] = mapped_column(String(50), nullable=False)
    fix_config: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Metadata
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual"
    )  # manual | llm | codified

    # Confidence tracking
    success_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    confidence_score: Mapped[float] = mapped_column(
        Numeric(5, 4), default=0.0, server_default="0.0", nullable=False
    )

    # Priority and state
    priority: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    promoted_to_code: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    promoted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<RemediationRule(id={self.id}, name='{self.name}', "
            f"error_code='{self.error_code}', source='{self.source}')>"
        )


class RemediationAttempt(Base):
    """Audit trail of each auto-fix attempt."""

    __tablename__ = "remediation_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    error_log_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("publish_error_log.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("remediation_rules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fix_type: Mapped[str] = mapped_column(String(50), nullable=False)
    fix_applied: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    modified_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    result: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # success | failure | different_error

    result_detail: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<RemediationAttempt(id={self.id}, error_log_id={self.error_log_id}, "
            f"result='{self.result}')>"
        )
