"""Registered callback watches and a capability-only transactional ingress.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0114_callback_watches"
down_revision = "0113_unattended_turn_provenance"
branch_labels = None
depends_on = None

# Keep this body frozen: migrations must not change when application code changes.
# The schema is fixed at installation, never supplied by the invoking identity.
ACCEPT_SQL = """
CREATE FUNCTION {schema}.accept_watch_callback(
    p_watch uuid, p_digest text, p_event text, p_payload text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {schema}, pg_temp AS $body$
DECLARE
    w watches%ROWTYPE;
    k uuid;
    t timestamptz := clock_timestamp();
    n integer;
BEGIN
    IF octet_length(p_payload) > 16384 OR length(p_event) NOT BETWEEN 1 AND 128
       OR p_payload IS NULL OR p_event IS NULL OR p_digest IS NULL THEN
        RAISE EXCEPTION 'callback refused' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO w FROM watches WHERE id = p_watch FOR UPDATE;
    IF NOT FOUND OR w.source <> 'callback' OR NOT EXISTS (
        SELECT 1 FROM watch_capabilities c WHERE c.watch_id = w.id
        AND c.digest = p_digest AND c.operation = 'deliver'
        AND c.revoked_at IS NULL AND c.expires_at > t
    ) THEN
        RAISE EXCEPTION 'callback refused' USING ERRCODE = '42501';
    END IF;
    -- A retry after acceptance returns its committed identity, including at expiry.
    SELECT wake_key INTO k FROM watch_inbox
        WHERE producer = w.producer AND event_id = p_event;
    IF FOUND THEN RETURN k; END IF;
    IF w.status <> 'active' OR w.expires_at <= t OR
       (w.max_fires IS NOT NULL AND w.fire_count >= w.max_fires) OR
       NOT EXISTS (SELECT 1 FROM accounts a JOIN tenants g ON g.id = a.tenant_id
          WHERE a.id = w.owner_account_id AND a.tenant_id = w.tenant_id
          AND g.archived_at IS NULL) OR
       NOT EXISTS (SELECT 1 FROM platform_principals p WHERE p.tenant_id = w.tenant_id
          AND p.platform = w.platform AND p.external_id = w.owner_external_id
          AND p.account_id = w.owner_account_id) OR
       NOT EXISTS (SELECT 1 FROM thread_sessions s WHERE s.tenant_id = w.tenant_id
          AND s.account_id = w.owner_account_id AND s.platform = w.platform
          AND s.thread_id = w.thread_id AND s.channel_id = w.parent_channel_id
          AND s.ma_agent_id = w.agent_id AND s.ma_session_id = w.session_id
          AND s.status = 'live') THEN
        RAISE EXCEPTION 'callback refused' USING ERRCODE = '42501';
    END IF;
    IF w.rate_window_at IS NULL OR w.rate_window_at < t - interval '1 minute' THEN
        UPDATE watches SET rate_window_at = t, rate_count = 0 WHERE id = w.id;
    ELSIF w.rate_count >= 60 THEN
        RAISE EXCEPTION 'callback rate exceeded' USING ERRCODE = '54000';
    END IF;
    -- Serialize owner caps across different watches, independently of wake leases.
    PERFORM pg_advisory_xact_lock(hashtextextended('watch:' || w.owner_account_id::text, 0));
    SELECT f.wake_key INTO k FROM watch_firings f JOIN task_continuations q
      ON q.idempotency_key = f.wake_key
      WHERE f.watch_id = w.id AND q.status = 'pending' AND q.started_at IS NULL
      ORDER BY f.created_at DESC LIMIT 1 FOR UPDATE OF q;
    IF k IS NULL THEN
        SELECT count(*) INTO n FROM watch_firings f WHERE f.watch_id = w.id
            AND f.created_at > t - interval '1 day';
        IF n >= least(w.max_turns_per_day, 24) THEN
            RAISE EXCEPTION 'watch daily cap' USING ERRCODE = '54000';
        END IF;
        SELECT count(*) INTO n FROM watch_firings f JOIN watches v ON v.id = f.watch_id
          WHERE v.tenant_id = w.tenant_id AND v.owner_account_id = w.owner_account_id
          AND f.created_at > t - interval '1 day';
        IF n >= 100 THEN RAISE EXCEPTION 'owner daily cap' USING ERRCODE = '54000'; END IF;
        k := gen_random_uuid();
        INSERT INTO task_continuations
          (tenant_id, platform, parent_channel_id, thread_id, requester_account_id,
           requester_external_user_id, target_ma_agent_id, target_name,
           requested_work, reason, idempotency_key, available_at)
        VALUES (w.tenant_id, w.platform, w.parent_channel_id, w.thread_id,
          w.owner_account_id, w.owner_external_id, w.agent_id, w.agent_name,
          'Registered callback evidence is ready.', 'watch_fired', k, t);
        INSERT INTO watch_firings(wake_key, watch_id) VALUES (k, w.id);
    END IF;
    INSERT INTO watch_inbox(watch_id, producer, event_id, payload, wake_key)
        VALUES(w.id, w.producer, p_event, p_payload, k);
    UPDATE watch_firings SET
      events = CASE WHEN jsonb_array_length(events) < 20 THEN
        events || jsonb_build_array(jsonb_build_object('event_id', p_event, 'text', p_payload))
        ELSE events END,
      omitted = omitted + CASE WHEN jsonb_array_length(events) >= 20 THEN 1 ELSE 0 END
      WHERE wake_key = k;
    UPDATE watches SET fire_count = fire_count + 1, rate_count = rate_count + 1 WHERE id = w.id;
    RETURN k;
END
$body$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # These declarations intentionally mirror only this revision's schema.
    op.create_table(
        "watches",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "owner_account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *(
            sa.Column(n, sa.Text(), nullable=False)
            for n in (
                "owner_external_id",
                "platform",
                "parent_channel_id",
                "thread_id",
                "agent_id",
                "agent_name",
                "session_id",
                "instructions",
                "producer",
            )
        ),
        sa.Column("source", sa.Text(), nullable=False, server_default="callback"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        *(
            sa.Column(n, JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"))
            for n in ("baseline", "rule", "source_state")
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        *(
            sa.Column(n, sa.DateTime(timezone=True))
            for n in ("lease_expires_at", "next_check_at", "backoff_until", "rate_window_at")
        ),
        *(
            sa.Column(n, sa.Integer(), nullable=False, server_default="0")
            for n in ("consecutive_errors", "fire_count", "rate_count")
        ),
        sa.Column("max_turns_per_day", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("max_fires", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("producer"),
    )
    op.create_table(
        "watch_capabilities",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "watch_id", sa.UUID(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("digest", sa.Text(), nullable=False, unique=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "watch_firings",
        sa.Column(
            "wake_key",
            sa.UUID(),
            sa.ForeignKey("task_continuations.idempotency_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "watch_id", sa.UUID(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("events", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("omitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_watch_firings_watch_id", "watch_firings", ["watch_id"])
    op.create_table(
        "watch_inbox",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "watch_id", sa.UUID(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
        ),
        *(sa.Column(n, sa.Text(), nullable=False) for n in ("producer", "event_id", "payload")),
        sa.Column(
            "wake_key",
            sa.UUID(),
            sa.ForeignKey("watch_firings.wake_key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("producer", "event_id", name="uq_watch_inbox_event"),
    )
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready', 'worker_finished', 'watch_fired')",
    )
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.execute(ACCEPT_SQL.format(schema=schema))
    # A non-login capability group; operators provision a lane-specific login
    # and grant this group, with no membership in the application owner's role.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
        "WHERE rolname = 'daimon_sidecar') THEN CREATE ROLE daimon_sidecar NOLOGIN; "
        "END IF; END $$"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {schema}.accept_watch_callback(uuid,text,text,text) FROM PUBLIC"
    )
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO daimon_sidecar")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {schema}.accept_watch_callback(uuid,text,text,text) "
        "TO daimon_sidecar"
    )


def downgrade() -> None:
    raise NotImplementedError("Callback evidence and delivery history must be retained")
