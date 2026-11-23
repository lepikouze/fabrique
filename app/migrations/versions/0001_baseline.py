"""baseline: the whole schema of Fabrique 0.8.0

0.8.0 is a clean break (every identifier moved to English): there is no upgrade path from 0.7.x, an installation starts
from an empty database. The sixteen earlier migrations are therefore replaced by this one. Later changes add
migrations on top of it, as before.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0001", None, None, None


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
    op.create_index(op.f('ix_notifications_state'), 'notifications', ['state'], unique=False)
    op.create_table('repos',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('git_id', sa.String(length=60), nullable=False),
    sa.Column('slug', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('default_branch', sa.String(length=80), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('accepts_jobs', sa.Boolean(), nullable=False),
    sa.Column('onboarding', sa.String(length=20), nullable=False),
    sa.Column('recipe', sa.JSON(), nullable=True),
    sa.Column('settings', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('last_activity', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('git_id', 'slug', name='uq_repos_git_slug')
    )
    op.create_table('runners',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('host', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('online', sa.Boolean(), nullable=False),
    sa.Column('last_seen', sa.Float(), nullable=True),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.Column('in_flight', sa.Integer(), nullable=False),
    sa.Column('harnesses', sa.JSON(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('version', sa.String(length=40), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('revoked', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('tags', sa.JSON(), server_default='[]', nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('settings',
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_by', sa.String(length=120), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('role_source', sa.String(length=40), nullable=False),
    sa.Column('requested_role', sa.String(length=20), nullable=True),
    sa.Column('request_note', sa.Text(), nullable=True),
    sa.Column('requested_at', sa.Float(), nullable=True),
    sa.Column('groups', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('last_login', sa.Float(), nullable=True),
    sa.Column('prefs', sa.JSON(), nullable=True),
    sa.Column('sessions_after', sa.Float(), nullable=True),
    sa.Column('password_hash', sa.String(length=300), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ref', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('request', sa.Text(), nullable=False),
    sa.Column('done_when', sa.Text(), nullable=False),
    sa.Column('requester', sa.String(length=120), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('repo_id', sa.Integer(), nullable=False),
    sa.Column('stage', sa.String(length=20), nullable=False),
    sa.Column('activity', sa.String(length=10), nullable=False),
    sa.Column('status_text', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('harness', sa.String(length=40), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('runner_id', sa.Integer(), nullable=True),
    sa.Column('branch', sa.String(length=120), nullable=False),
    sa.Column('pr_number', sa.Integer(), nullable=True),
    sa.Column('pr_url', sa.String(length=300), nullable=True),
    sa.Column('cost', sa.Float(), nullable=False),
    sa.Column('tokens', sa.Integer(), nullable=False),
    sa.Column('budget', sa.Float(), nullable=False),
    sa.Column('verified_sha', sa.String(length=40), nullable=True),
    sa.Column('checks_ok', sa.Integer(), nullable=False),
    sa.Column('checks_total', sa.Integer(), nullable=False),
    sa.Column('merge_state', sa.String(length=20), nullable=False),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('report', sa.Text(), nullable=True),
    sa.Column('lang', sa.String(length=8), nullable=False),
    sa.Column('models', sa.JSON(), nullable=True),
    sa.Column('report_seen', sa.Float(), nullable=True),
    sa.Column('session_id', sa.String(length=80), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('diff', sa.JSON(), nullable=True),
    sa.Column('cancel_requested', sa.String(length=120), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repos.id'], ),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ref')
    )
    op.create_index('ix_jobs_stage', 'jobs', ['stage'], unique=False)
    op.create_table('agents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('task', sa.String(length=240), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('tokens', sa.Integer(), nullable=False),
    sa.Column('cost', sa.Float(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agents_job_id'), 'agents', ['job_id'], unique=False)
    op.create_table('audit',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.Column('actor', sa.String(length=120), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('target', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_at'), 'audit', ['at'], unique=False)
    op.create_table('decisions',
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('policy', sa.String(length=20), nullable=False),
    sa.Column('recommendation', sa.String(length=240), nullable=False),
    sa.Column('interpretation', sa.Text(), nullable=False),
    sa.Column('assumptions', sa.JSON(), nullable=False),
    sa.Column('confident', sa.Boolean(), nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('limits', sa.JSON(), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('job_id')
    )
    op.create_table('diffs',
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('sha', sa.String(length=40), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('truncated', sa.Boolean(), nullable=False),
    sa.Column('files', sa.JSON(), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('job_id')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('at', sa.Float(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_events_job_id'), 'events', ['job_id'], unique=False)
    op.create_table('evidence',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('sha', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('command', sa.String(length=300), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('output', sa.Text(), nullable=False),
    sa.Column('ran_by', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('warn', sa.Boolean(), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_evidence_job_id'), 'evidence', ['job_id'], unique=False)
    op.create_table('intents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('runner_id', sa.Integer(), nullable=True),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('done_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_intents_job_id'), 'intents', ['job_id'], unique=False)
    op.create_table('questions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('asked_at', sa.Float(), nullable=False),
    sa.Column('answer', sa.Text(), nullable=True),
    sa.Column('answered_by', sa.String(length=120), nullable=True),
    sa.Column('answered_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_questions_job_id'), 'questions', ['job_id'], unique=False)
    op.create_table('reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('sha', sa.String(length=40), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('verdict', sa.String(length=20), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('points', sa.JSON(), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reviews_job_id'), 'reviews', ['job_id'], unique=False)
    op.create_table('timeline',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('stage', sa.String(length=20), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_timeline_job_id'), 'timeline', ['job_id'], unique=False)
    op.create_table('translations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('lang', sa.String(length=8), nullable=False),
    sa.Column('source_hash', sa.String(length=64), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('cost', sa.Float(), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'lang', 'source_hash', name='uq_translations_job_lang_source')
    )
    op.create_index(op.f('ix_translations_job_id'), 'translations', ['job_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_translations_job_id'), table_name='translations')
    op.drop_table('translations')
    op.drop_index(op.f('ix_timeline_job_id'), table_name='timeline')
    op.drop_table('timeline')
    op.drop_index(op.f('ix_reviews_job_id'), table_name='reviews')
    op.drop_table('reviews')
    op.drop_index(op.f('ix_questions_job_id'), table_name='questions')
    op.drop_table('questions')
    op.drop_index(op.f('ix_intents_job_id'), table_name='intents')
    op.drop_table('intents')
    op.drop_index(op.f('ix_evidence_job_id'), table_name='evidence')
    op.drop_table('evidence')
    op.drop_index(op.f('ix_events_job_id'), table_name='events')
    op.drop_table('events')
    op.drop_table('diffs')
    op.drop_table('decisions')
    op.drop_index(op.f('ix_audit_at'), table_name='audit')
    op.drop_table('audit')
    op.drop_index(op.f('ix_agents_job_id'), table_name='agents')
    op.drop_table('agents')
    op.drop_index('ix_jobs_stage', table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('users')
    op.drop_table('settings')
    op.drop_table('runners')
    op.drop_table('repos')
    op.drop_index(op.f('ix_notifications_state'), table_name='notifications')
    op.drop_table('notifications')
