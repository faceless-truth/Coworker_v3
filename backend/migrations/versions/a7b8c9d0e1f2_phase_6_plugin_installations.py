"""Phase 6-1: plugin_installations

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-13 04:00:00.000000

Per-firm plugin enrolment. One row per (firm, plugin_name) pair
tracking the version installed, the firm-specific config, and the
operational flags (enabled, dry_run). The plugin's static metadata
(default config schema, declared triggers, etc.) lives in Python;
the DB only stores per-firm decisions.

RLS+FORCE — Phase 6's scheduler walks every firm's installations
to decide what to run; that walk goes through firm_context, so
the rows isolate correctly without join.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RLS_MATCH = "firm_id = NULLIF(current_setting('app.firm_id', true), '')::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for action in ("select", "insert", "update", "delete"):
        if action == "insert":
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR INSERT WITH CHECK ({_RLS_MATCH})"
            )
        elif action == "update":
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR UPDATE USING ({_RLS_MATCH}) WITH CHECK ({_RLS_MATCH})"
            )
        else:
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR {action.upper()} USING ({_RLS_MATCH})"
            )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE plugin_installations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            plugin_name VARCHAR(100) NOT NULL,
            plugin_version VARCHAR(50) NOT NULL,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            is_dry_run BOOLEAN NOT NULL DEFAULT FALSE,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT plugin_installations_firm_plugin_unique
                UNIQUE (firm_id, plugin_name)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_plugin_installations_firm_enabled "
        "ON plugin_installations (firm_id, is_enabled) "
        "WHERE is_enabled = TRUE"
    )
    _enable_rls("plugin_installations")


def downgrade() -> None:
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"plugin_installations_firm_isolation_{op_name} "
            f"ON plugin_installations"
        )
    op.execute("DROP TABLE IF EXISTS plugin_installations CASCADE")
