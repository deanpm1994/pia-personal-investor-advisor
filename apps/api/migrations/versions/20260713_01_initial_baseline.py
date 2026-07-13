"""Establish the application migration baseline without domain tables.

Revision ID: 20260713_01
Revises: None
Create Date: 2026-07-13 00:00:00
"""

from collections.abc import Sequence

revision: str = "20260713_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record the Alembic baseline; P2.1 creates no application tables."""


def downgrade() -> None:
    """Revert the empty baseline; Alembic removes its version table as needed."""
