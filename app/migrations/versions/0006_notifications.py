"""notifications : file d'envoi

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('event', sa.String(length=30), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('next_try', sa.Float(), nullable=False),
    sa.Column('sent_at', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_state'), ['state'], unique=False)


def downgrade():
    op.drop_table('notifications')
