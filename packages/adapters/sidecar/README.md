# Daimon page-event sidecar

`daimon-sidecar` is a bounded callback adapter with no provider client, turn runner,
polling or MCP management tools. Core owns registration, replay/evidence storage,
durable wakes and stop controls. The sidecar has no direct table grants.

Both producer gates default off pending release review. Migration `0117_watches`
follows `0116_thread_model_settings`. Enrollment binds an existing Discord thread;
watch turns need its current unattended execution grant, recheck live access and
spend, and only read/report. Original tools restore before release, including
cancellation; cache invalidation forces reconciliation after interruption.

Authorized OS page hosts call `daimon.core.continuity.page_events.sign_page_event`
after ACL/CSRF checks. HMAC SHA-256 signs newline-separated UTF-8 fields:
`daimon-page-v1`, watch UUID, page UUID, key ID, Unix seconds, event UUID and the raw
body's SHA-256 hex digest. UUIDs use canonical lowercase spelling; key IDs use
ASCII letters, digits, `_` and `-`. Random keys remain in secret management; only
hashes are registered. Signed page text is still untrusted evidence.

POST the body and signed headers to `/callbacks/watches/{watch_id}`. A 202 confirms
durable acceptance; 409 indicates replay. Retries keep the event UUID and refresh
the signed timestamp. Other paths remain private or absent, and health checks do
not enqueue work. Evidence expires after seven days; a fresh event after expiry
starts a new wake rather than joining an unreadable old batch.

See [self-hosting](../../../docs/self-hosting.md#signed-page-event-watch-sidecar) for
configuration, signing headers, limits and database roles, and the
[lane guide](../../../deploy/lanes/README.md#signed-page-event-watch-sidecar) for the
optional preview profile. There are no outside senders or internal-watch migration.
