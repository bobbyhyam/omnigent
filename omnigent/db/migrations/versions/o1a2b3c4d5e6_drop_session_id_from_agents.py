"""drop session_id column from agents

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Removes ``agents.session_id`` — the back-pointer from an agent row to
the conversation that owns it. The forward pointer
``conversations.agent_id`` is the single source of truth: an agent is
"session-scoped" if some conversation row references it, and
"template" (built-in) otherwise. Queries that previously filtered on
``session_id IS NULL`` are rewritten to use a NOT EXISTS subquery
against ``conversations.agent_id``.

The partial unique index ``ix_agents_template_name`` (scoped to
``session_id IS NULL``) is also dropped and recreated without the
WHERE clause predicate because the semantics are now captured in the
NOT EXISTS queries rather than in the schema.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)


def upgrade() -> None:
    """
    Drop ``agents.session_id``, its FK, and its indexes.

    The partial unique index ``ix_agents_template_name`` is recreated
    without the ``WHERE session_id IS NULL`` predicate because the
    session-scope distinction is no longer enforced at the schema level;
    it is derived at query time via NOT EXISTS against
    ``conversations.agent_id``.
    """
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_index("ix_agents_template_name")
        batch_op.drop_index("ix_agents_session_id")
        batch_op.drop_constraint("fk_agents_session_id", type_="foreignkey")
        batch_op.drop_column("session_id")
        batch_op.create_index(
            "ix_agents_template_name",
            ["name"],
            unique=True,
        )


def downgrade() -> None:
    """
    Re-add ``agents.session_id`` and back-populate from conversations.

    Session-scoped agents (those referenced by a conversation via
    ``conversations.agent_id``) get their owning conversation id
    written back; template agents get ``NULL``. The partial
    ``ix_agents_template_name`` index is restored.
    """
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_index("ix_agents_template_name")
        batch_op.add_column(
            sa.Column("session_id", sa.String(length=64), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_agents_session_id",
            "conversations",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_agents_session_id",
            ["session_id"],
            unique=True,
        )
        batch_op.create_index(
            "ix_agents_template_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("session_id IS NULL"),
            postgresql_where=sa.text("session_id IS NULL"),
        )

    # Back-populate session_id from conversations.agent_id.
    op.execute(
        sa.text(
            "UPDATE agents SET session_id = ("
            "  SELECT id FROM conversations WHERE conversations.agent_id = agents.id LIMIT 1"
            ")"
        )
    )
    _logger.info("Downgrade: back-populated agents.session_id from conversations.agent_id")
