"""RuntimeConfig model — global key-value settings configurable from the admin UI."""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeConfig(Base):
    """Global runtime configuration stored in DB, editable without restart."""

    __tablename__ = "runtime_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_secret: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

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
        return f"<RuntimeConfig(key='{self.key}', is_secret={self.is_secret})>"
