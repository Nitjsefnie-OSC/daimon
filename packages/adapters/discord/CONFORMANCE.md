# Discord host conformance

Import `daimon.adapters.discord.conformance` explicitly in an offline host
envelope. The application does not import it. Use a dedicated idle `DaimonBot`;
the probe temporarily captures its mention handler and restores it on success,
callback failure, timeout or cancellation. It never attaches to a live gateway.

`MultiHumanEnvelope` supplies the bot, tenant UUID, guild ID, channel ID, at
least two `ThreadInputs(first, queued)` under that channel, and a `run` callback.
Messages must have unique IDs and at least two human authors. Each thread has
queued follow-ups. `timeout_s` is a finite positive overall deadline.

`run(MentionTurn, AdmissionBarrier | None)` receives the exact actual message,
guild/tenant and content/attachment overrides passed by Discord's drain. The
first call per thread gets a barrier. Its host transport signals `started`
after normal admission and actual root acceptance; it waits for `release`
before completing that root. The probe enqueues follow-ups through the actual
reaction/queue method, releases the root and invokes the actual drain method.
Save the original bound mention handler before the probe if forwarding to it;
forward all captured parameters unchanged. The callback must perform normal
admission/binding/execution; it must not hand-pin admissions or return verdicts.
The barrier itself is a scheduling control, not an acceptance receipt.

The returned mapping contains `platform`, `threads`, `batches`,
`peak_callbacks_by_thread`, `queue_empty_after` and `processing_empty_after`.
Thread facts record received and queued message IDs, claimed ownership and
whether the first callback finished before queuing. Batch facts record the
thread, first message, caller, actual composed content, override flag and
attachment IDs. Callback overlap is not a native active-root count.

The shared C01 oracle also needs independent actual host/provider facts:
same configuration and credential principal in a channel, distinct workspaces
across its two threads, one active root per thread, and caller-attributed
charges. In opt-in shared mode, callers share their thread workspace; they
never share the other thread's workspace. Native journal, prepared bindings
and actual persisted usage/ledger facts must prove these separately. A queue
control with synthetic callbacks is not full C01 or live certification. Missing
provider choreography and shared-runner activation remain typed PENDING.

The controls use the actual queue/drain/reaction paths and parsed Discord SDK
messages, threads, users and attachments. They also reject invalid/busy inputs
before callback side effects and check restoration/cleanup. No provider calls,
SDK fields, usage rows, golden files or default behavior are fabricated.
