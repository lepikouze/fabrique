"""comptes locaux : mot de passe (haché) pour le compte admin de secours

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0007", "0006", None, None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("password_hash", sa.String(300), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("password_hash")
