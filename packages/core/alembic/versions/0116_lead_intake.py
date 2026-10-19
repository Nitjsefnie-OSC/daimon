"""Encrypted tenant-scoped lead ledger, effect intents and human audit.

downgrade: unsupported
"""

from alembic import op

revision = "0116_lead_intake"
down_revision = "0113_unattended_turn_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    statements = """
        CREATE TABLE lead_intake_configs (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            config JSONB NOT NULL, encrypted_credentials BYTEA NOT NULL);
        CREATE TABLE lead_intakes (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL, source_digest TEXT NOT NULL, status TEXT NOT NULL,
            encrypted_payload BYTEA, encrypted_raw BYTEA,
            created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            raw_expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_lead_source UNIQUE (tenant_id, source_digest));
        CREATE TABLE lead_intake_effects (
            tenant_id UUID NOT NULL, intake_id UUID NOT NULL, step TEXT NOT NULL,
            stable_key TEXT NOT NULL, state TEXT NOT NULL, remote_id TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, intake_id, step),
            FOREIGN KEY (tenant_id, intake_id) REFERENCES lead_intakes(tenant_id, id)
            ON DELETE CASCADE);
        CREATE TABLE lead_resolution_audit (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, intake_id UUID NOT NULL,
            encrypted_resolution BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL,
            FOREIGN KEY (tenant_id, intake_id) REFERENCES lead_intakes(tenant_id, id)
            ON DELETE CASCADE);
        CREATE TABLE lead_mailbox_states (
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            connection_id TEXT NOT NULL, encrypted_state BYTEA NOT NULL,
            PRIMARY KEY (tenant_id, connection_id));
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Lead evidence requires an explicit retention/export procedure")
