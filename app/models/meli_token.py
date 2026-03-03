"""MeliToken model - OAuth tokens per user."""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MeliToken(Base):
    __tablename__ = "meli_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # user_id is a logical FK to the backend's users table (no FK constraint across DBs)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    access_token: Mapped[str] = mapped_column(String(500), nullable=False)
    refresh_token: Mapped[str] = mapped_column(String(500), nullable=False)
    token_type: Mapped[str] = mapped_column(String(20), default="Bearer", nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    meli_user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<MeliToken(id={self.id}, user_id={self.user_id}, meli_user_id='{self.meli_user_id}')>"
