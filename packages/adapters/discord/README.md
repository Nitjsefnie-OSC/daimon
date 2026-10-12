# Discord adapter

Gateway lease tests synchronize with PostgreSQL backend shutdown before a
successor attempts the advisory lock. The termination helper waits within a
bounded deadline for the old PID to disappear from activity and lock records;
the successor's lock attempt and fail-stop assertions remain single attempts.
The takeover test also gates the old holder's reconnect check until the successor
has acquired the lock, then calls the real check to prove the old holder stops.

The output-delivery module exposes `deliver_provider_output` for verified neutral
provider bytes. The caller must validate the admitted tenant, provider session
and authoritative root before supplying a `DeliverableFile`. This edge does
not select outputs or fetch provider content; host/event wiring is separate.

Uploads reuse answer attachment edits and standalone `discord.File` fallback.
Existing answer attachments are preserved. The guild size limit and the current
writer protection still apply.

The `DaimonBot` constructor accepts explicit optional `http_client` and UTC
`clock` dependencies for in-process tests. Ordinary construction retains the
existing HTTP client and clock. The explicit `daimon.testing.discord_ingress`
helper drives original gateway messages through real ingress/admission and
observes title/queue/HTTP evidence; see `tests/GATEWAY-INGRESS.md` for its owned
runtime contract and the separate native completion requirement.

This title/admission slice rejects injected provider runtimes, backends and
stores at the observation boundary. Captured Anthropic fixture completion is
separate from independently pinned alternate-provider completion.


`DiscordConcurrentDriver` extends the opt-in testing ingress with real queue,
reaction, binding/lease and card checkpoints for concurrent busy-thread and
owned tenant-cap probes. See [the ingress contract](tests/GATEWAY-INGRESS.md).
Native gates and independent scenario verdicts remain runner-owned.

Ingress receipts also expose actual HTTP message-fetch replies and discord.py's
parsed message references. Channel bursts retain each authored identity and
separate production admission; chunk replies bind to an actually emitted SDK
message. See the source-pinned burst/chunk controls in the ingress contract.

The posted-card renderer accepts optional invoker-bound `controls`. Callbacks
come from the authenticated operation owner, which must recheck current
authority and version before writing. They return whether the write was
accepted; that is not a worker completion or review verdict. A refusal keeps
controls available, while accepted writes retire the old controls. This uses
the existing Stop guard and missing-message boundary. Calls without controls
retain their existing serialized components. Fleet status and Resume service
binding are separate from this rendering seam.
