"""Phase 9-7: approval_items.confidence

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-05-14 14:45:00.000000

Plugins now stamp a self-rated confidence onto each approval
item. The helper auto-approves rows where:

- ``confidence >= firm.auto_approve_threshold``, AND
- ``required_approvals == 1`` (two-person categories always
  require human signatures — auto-approve cannot bypass the
  high-sensitivity guard).

Auto-approved rows record a system signature
``{"user_id": null, "signed_at": now, "notes": "auto:
 confidence=X.XX"}`` instead of a real reviewer.

Nullable: the column is meaningful only when a plugin chose to
self-rate. NULL preserves the pre-9-7 behaviour (always route
to human approval).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN confidence DOUBLE PRECISION "
        "CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items DROP COLUMN IF EXISTS confidence"
    )
