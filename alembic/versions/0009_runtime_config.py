"""Add runtime_config table for UI-configurable settings.

Revision ID: 0009_runtime_config
Revises: 0008_error_learning
Create Date: 2026-03-28
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_runtime_config"
down_revision = "0008_error_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(100), unique=True, nullable=False, index=True),
        sa.Column("value", sa.Text, nullable=False, server_default=""),
        sa.Column("is_secret", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("updated_by", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_config")
