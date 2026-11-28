"""Record the vCPUs and memory that a host was asked for."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_host_cpu_memory"
down_revision: str | None = "0009_template_build_step"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("cpu", sa.Integer(), nullable=True))
    op.add_column("hosts", sa.Column("memory_gb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "memory_gb")
    op.drop_column("hosts", "cpu")
