# Daimon page-event sidecar

`daimon-sidecar` is a bounded HTTP event-source adapter. It depends on core and
runs no turns or model calls. It schedules reviewed reads through the OS executor.
Core owns registration, signature/replay checks, evidence, durable wakes and stop controls.
The sidecar has no direct table grants and receives a separate environment.

The application producer flag and durable database gate default off pending release
review. Outside callback senders are unsupported.

See [self-hosting](../../../docs/self-hosting.md#signed-page-event-watch-sidecar)
for callback signing, headers, replay/retention rules, database roles and limits.
The [lane guide](../../../deploy/lanes/README.md#signed-page-event-watch-sidecar)
covers the optional preview profile and exact enable/parity commands.

Polling uses a fixed private OS executor, never external URLs. Sidecar can claim
four 60-second poll leases at a time; the private executor loads the registered
source and checks live owner permissions. Only OS can submit poll results. Both
producer gates remain default off. See the lane README for owner tools, offline
paused imports and the five-step preview procedure.

The poll-only ASGI app runs separately on `watch-executor:8767`, without published
ports or proxy routes. The public MCP app has no executor route. Configure the
same random secret in `DAIMON_WATCHES__EXECUTOR_SECRET` on the executor and
`DAIMON_SIDECAR__EXECUTOR_SECRET` on sidecar. Missing secrets disable transport;
invalid secrets and leases cannot consume the shared real-poll budget. Local
processes use only `http://127.0.0.1:8767` and bind that listener to loopback.
