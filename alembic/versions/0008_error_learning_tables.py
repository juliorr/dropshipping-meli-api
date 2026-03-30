"""Add error learning and auto-remediation tables.

Revision ID: 0008_error_learning
Revises: 0007_unique_active_per_product
Create Date: 2026-03-28
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_error_learning"
down_revision = "0007_unique_active_per_product"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- remediation_rules (must be created first — referenced by FK) ---
    op.create_table(
        "remediation_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("error_code", sa.String(100), nullable=False, index=True),
        sa.Column("cause_code", sa.String(100), nullable=True),
        sa.Column("error_pattern", sa.String(500), nullable=True),
        sa.Column("category_pattern", sa.String(100), nullable=True),
        sa.Column("fix_type", sa.String(50), nullable=False),
        sa.Column("fix_config", sa.JSON, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "confidence_score", sa.Numeric(5, 4), nullable=False, server_default="0.0"
        ),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "promoted_to_code", sa.Boolean, nullable=False, server_default="false"
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
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

    # --- publish_error_log ---
    op.create_table(
        "publish_error_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, nullable=False, index=True),
        sa.Column("listing_id", sa.Integer, nullable=False, index=True),
        sa.Column("product_id", sa.Integer, nullable=False, index=True),
        sa.Column("meli_category_id", sa.String(20), nullable=True, index=True),
        sa.Column("error_code", sa.String(100), nullable=False, index=True),
        sa.Column("error_message", sa.Text, nullable=False),
        sa.Column("ml_response", sa.JSON, nullable=False),
        sa.Column("publish_payload", sa.JSON, nullable=False),
        sa.Column("request_data", sa.JSON, nullable=True),
        sa.Column(
            "remediation_attempted",
            sa.Boolean,
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "remediation_rule_id",
            sa.Integer,
            sa.ForeignKey("remediation_rules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("remediation_succeeded", sa.Boolean, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- remediation_attempts ---
    op.create_table(
        "remediation_attempts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "error_log_id",
            sa.Integer,
            sa.ForeignKey("publish_error_log.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "rule_id",
            sa.Integer,
            sa.ForeignKey("remediation_rules.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("fix_type", sa.String(50), nullable=False),
        sa.Column("fix_applied", sa.JSON, nullable=False),
        sa.Column("modified_payload", sa.JSON, nullable=True),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("result_detail", sa.JSON, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("remediation_attempts")
    op.drop_table("publish_error_log")
    op.drop_table("remediation_rules")
