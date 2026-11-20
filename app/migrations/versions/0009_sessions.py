"""sessions révocables : instant avant lequel les sessions d'un compte ne valent plus

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0009", "0008", None, None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("sessions_after", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("sessions_after")
