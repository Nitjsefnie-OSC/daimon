# Daimon page-event sidecar

`daimon-sidecar` is a bounded HTTP event-source adapter. It depends on core and
runs no turns or model calls. It schedules reviewed reads through the OS executor. Core owns the
registration, signature/replay checks, evidence, durable wake and stop controls.
The sidecar has no direct table grants and receives a separate environment.

The application producer flag and durable database gate default off. Keep them
off until execution-bound origin grants and cleanup safety have passed review.
See `deploy/lanes/README.md` for the optional preview profile and constrained role
setup. Outside callback senders are unsupported.

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
optional preview profile. Outside senders are unsupported.

Polling uses a fixed private OS executor, never external URLs. Sidecar can claim
four 60-second poll leases at a time; the OS MCP executor loads the registered
source and checks live owner permissions. Only OS can submit poll results. Both
producer gates remain default off. See the lane README for owner tools, offline
paused imports and the five-step preview procedure.
