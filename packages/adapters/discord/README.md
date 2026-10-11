# Discord adapter

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
