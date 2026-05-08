"""dashboard_login_codes

Revision ID: 003_dashboard_login_codes
Revises: 002_blacklist_per_user
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa

revision = '003_dashboard_login_codes'
down_revision = '002_blacklist_per_user'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_login_codes (
            code VARCHAR(10) PRIMARY KEY,
            user_id BIGINT,
            username VARCHAR(255),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            claimed_at TIMESTAMPTZ,
            consumed_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_dashboard_login_codes_expires_at ON dashboard_login_codes (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dashboard_login_codes")
