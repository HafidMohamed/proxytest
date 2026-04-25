"""v2 schema: hashed API keys, routing mode, translation memory, glossary, usage events

Revision ID: 001_v2_schema
Revises: 
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid

revision = '001_v2_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Add new columns to customers ──────────────────────────────────────
    op.add_column('customers', sa.Column('api_key_hash', sa.String(128), nullable=True))
    op.add_column('customers', sa.Column('api_key_prefix', sa.String(12), nullable=True))
    op.add_column('customers', sa.Column('plan', sa.String(32), server_default='free'))
    op.add_column('customers', sa.Column('monthly_word_limit', sa.Integer(), server_default='2000'))

    # Migrate existing plaintext api_keys to hashed format
    op.execute("""
        UPDATE customers
        SET api_key_hash   = encode(sha256(api_key::bytea), 'hex'),
            api_key_prefix = 'sk-' || left(api_key, 8)
        WHERE api_key_hash IS NULL
    """)

    op.alter_column('customers', 'api_key_hash', nullable=False)
    op.alter_column('customers', 'api_key_prefix', nullable=False)
    op.create_unique_constraint('uq_customers_api_key_hash', 'customers', ['api_key_hash'])
    op.create_index('ix_customers_api_key_hash', 'customers', ['api_key_hash'])

    # ── Add routing_mode to domains ───────────────────────────────────────
    op.execute("CREATE TYPE routingmode AS ENUM ('subdirectory', 'subdomain')")
    op.add_column('domains', sa.Column('routing_mode',
        sa.Enum('subdirectory', 'subdomain', name='routingmode'),
        server_default='subdirectory'))

    # ── Add routing_mode + original_lang to translation_configs ───────────
    op.add_column('translation_configs', sa.Column('routing_mode',
        sa.Enum('subdirectory', 'subdomain', name='routingmode'),
        server_default='subdirectory'))

    # ── Add word_count + html_url to translated_pages ─────────────────────
    op.add_column('translated_pages', sa.Column('word_count', sa.Integer(), server_default='0'))
    op.add_column('translated_pages', sa.Column('html_url', sa.String(2048), nullable=True))
    op.create_index('ix_translated_pages_config_lang', 'translated_pages', ['config_id', 'language'])

    # ── Translation Memory ─────────────────────────────────────────────────
    op.create_table(
        'translation_memory',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('source_text', sa.Text(), nullable=False),
        sa.Column('language', sa.String(10), nullable=False),
        sa.Column('translated_text', sa.Text(), nullable=False),
        sa.Column('hit_count', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_unique_constraint('uq_tm_hash_lang', 'translation_memory', ['source_hash', 'language'])
    op.create_index('ix_tm_hash_lang', 'translation_memory', ['source_hash', 'language'])

    # ── Glossary Rules ─────────────────────────────────────────────────────
    op.create_table(
        'glossary_rules',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('domain_id', UUID(as_uuid=True), sa.ForeignKey('domains.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_term', sa.String(512), nullable=False),
        sa.Column('language', sa.String(10), nullable=True),
        sa.Column('replacement', sa.String(512), nullable=True),
        sa.Column('case_sensitive', sa.Boolean(), server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_glossary_domain_lang', 'glossary_rules', ['domain_id', 'language'])

    # ── Usage Events ───────────────────────────────────────────────────────
    op.execute("CREATE TYPE usageeventtype AS ENUM ('words_translated', 'page_served', 'crawl_run')")
    op.create_table(
        'usage_events',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('customer_id', UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('domain_id', UUID(as_uuid=True), sa.ForeignKey('domains.id', ondelete='SET NULL'), nullable=True),
        sa.Column('event_type', sa.Enum('words_translated', 'page_served', 'crawl_run', name='usageeventtype'), nullable=False),
        sa.Column('quantity', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('language', sa.String(10), nullable=True),
        sa.Column('url', sa.String(2048), nullable=True),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_usage_customer_month', 'usage_events', ['customer_id', 'occurred_at'])


def downgrade() -> None:
    op.drop_table('usage_events')
    op.execute('DROP TYPE IF EXISTS usageeventtype')
    op.drop_table('glossary_rules')
    op.drop_table('translation_memory')
    op.drop_column('translated_pages', 'html_url')
    op.drop_column('translated_pages', 'word_count')
    op.drop_column('translation_configs', 'routing_mode')
    op.drop_column('domains', 'routing_mode')
    op.execute('DROP TYPE IF EXISTS routingmode')
    op.drop_column('customers', 'monthly_word_limit')
    op.drop_column('customers', 'plan')
    op.drop_column('customers', 'api_key_prefix')
    op.drop_column('customers', 'api_key_hash')
