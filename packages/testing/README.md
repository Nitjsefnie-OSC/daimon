# Discord surface capture

`daimon.testing.discord_surface.DiscordSurfaceHarness` captures offline Discord
posts through the unchanged `DiscordTurnLifecycle` and `DiscordPostTransport`.
Install the Discord adapter alongside `daimon-testing` (the workspace already
includes it). Import this module explicitly; ordinary testing imports do not
require Discord.

The harness creates real `discord.py` client, thread and message objects, with an
in-process HTTP gateway replacing their network boundary. It never logs in,
reads credentials or opens a Discord connection. Unsupported gateway operations
raise rather than falling through to a real client. Provider traffic in a test
must separately use its offline transport.

```python
from daimon.testing.discord_surface import DiscordSurfaceHarness

capture = DiscordSurfaceHarness(
    evidence_id="scenario:turn:discord",
    session_id=acknowledged_session_id,
    root_turn_id=acknowledged_root_id,
    clock=runner_clock,
    render_tables=True,
)

async def invoke(lifecycle):
    await lifecycle.post_initial()
    return await offline_host_turn(lifecycle=lifecycle)

receipt = await capture.run(invoke)
```

Bind the lifecycle to the actual turn invocation. The invocation's returned value
is ignored. The runner must independently match session/root identity to its
acknowledged input and native terminal evidence; this capture does not prove
provider completion. A single successful adapter terminal delivery followed by
the invocation returning closes the post window. A missing or duplicate terminal,
exception or cancellation leaves coverage incomplete. `snapshot()` retains
partial observations after failure; a harness cannot be reused for a second turn.

`events` contains immutable, ordered send/edit/delete observations, unique
capture evidence IDs, actual serialized requests and resulting message payloads.
`messages` contains surviving messages in Discord post order, with exact content,
embed text, component labels, attachment names, message IDs and observation times.
Edits replace only supplied fields; deletion removes the message from the final
snapshot. Drafts remain in history but do not reappear in finalized text. Footers
and cards remain separate from message content so the consumer can select its
explicit predicate domain instead of treating a headless return as a Discord post.

`post_capture_complete` covers this accepting fake gateway's message boundary.
`text_capture_complete` additionally refuses complete coverage when a surviving
attachment could contain unobserved text. Table PNGs are captured as files, never
converted into fabricated visible strings. Neither flag certifies browser pixels,
real gateway timing, permissions, webhook identity, reactions, tool posts outside
this gateway, or the full bot ingress/admission path. The default bot transport
path is exercised; identity webhooks are explicitly disabled. Configure the
lifecycle's table, notification, cancel-view and unprompted options to match the
scenario before comparing its output. Account balance footers require the host's
DB context and are outside this standalone harness. No product defaults change.

N9 binds its `VisibleText` from these observations only after matching the owned
capture window and identity. Keep predicates depending on uncaptured domains
PENDING. Native outcomes and headless receipts remain independent evidence.

## Owned database session factories

Call `daimon.testing.db.require_owned_sessionmaker(factory_or_engine)` before
opening a test-hook session. It validates the main bind and every routed
`binds` value, including connection binds' engines, without connecting or
changing the factory. Only local PostgreSQL `daimon_test_nc_*` lane databases
are allowed. The `daimon_test` exception requires both `GITHUB_ACTIONS=true`
and `CI=true`, plus `build_test_engine` provenance for a current-process
`test_w<pid>_<8hex>` or `test_f<pid>_<8hex>` schema on every engine. Execution
options and engine clones do not establish that provenance. Unbound factories,
foreign or synchronous binds, URL overrides and malformed routing maps fail
before I/O.

Guard adopters refuse plain local `daimon_test` by design. Their real-database
policy tests skip that documented contributor setup outside GitHub Actions CI;
use an owned lane database to exercise those tests locally.

Shared database fixtures also require the session-scoped `db_preflight` supplied
by the testing package's `pytest11` plugin, keyed by the resolved `db_schema`
value. Re-exported or overridden schemas receive separate admission
even when multiple packages run in one worker. It checks the DSN and process-owned
schema before `db_engine`, `db_nullpool_engine`, `db_clean` or session setup.
Each constructed engine then passes the same registered-schema ownership guard
before connecting. Valid lane/CI fixtures retain their pools, schema isolation,
cleanup and transactions. Plain local `daimon_test` skips before engine creation;
other foreign targets fail before I/O. Existing conftest imports need no edits.
Install the package/plugin with the usual `uv sync --all-extras --all-packages`;
when plugin autoload is disabled, pass `-p daimon.testing.pytest_plugin` explicitly.
Database-free tests do not request the preflight or need a database URL.

# Isolated provider SDK clients

`daimon.testing.isolated_sdk` supplies `build_async_openai`,
`build_async_anthropic` and `build_gemini_client` for tests. Each requires an
explicit HTTPX transport and supplies a fake API key, a fixed provider base URL
and empty custom headers by default. Override `api_key`, `base_url` and `headers`
explicitly when a test needs a different wire shape. HTTP clients use
`trust_env=False`; Gemini also receives explicit SSL contexts. Install the
corresponding OpenAI or Google GenAI SDK alongside this package to use those
factories; imports remain lazy.

```python
import httpx
from daimon.testing.isolated_sdk import build_async_openai

transport = httpx.MockTransport(
    lambda request: httpx.Response(200, json={"data": [], "object": "list"})
)
async with build_async_openai(transport=transport) as client:
    models = await client.models.list()
```

Construction raises `AmbientSDKConfigurationError` if any variable in
`SDK_ENV_VARS` is present, even with an empty value and even for another provider.
Every factory additionally refuses unknown `ANTHROPIC_`/`OPENAI_`/`GOOGLE_`/`GEMINI_`
variable names (case insensitive), including empty or future settings; unknown
names and all values are omitted from errors. The same check guards late OpenAI
and Anthropic SDK copies. Checks iterate names before set membership,
avoiding `os.environ` membership's implicit value lookup. Both `GOOGLE_API_KEY`
and `GEMINI_API_KEY` are refused before Gemini construction, even with an explicit
test key, because Google GenAI 2.7 reads them unconditionally. This includes
provider keys, webhook/admin keys, org/project settings, custom headers, base
URLs, Anthropic identity/configuration and Gemini credential,
Vertex and replay settings. Errors name variables without exposing values. The
factories never clear the process environment; explicit test keys prevent
credential-chain fallback. Start tests in a clean environment or remove only
the relevant variables with a scoped test fixture. Missing keys, routes or
transports also fail before HTTP client construction. Generic proxy and certificate environment settings are
refused before construction. The names-only guard additionally rejects
`SSL_CERT_FILE`, `SSL_CERT_DIR`, `SSLKEYLOGFILE`, `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE`, including
empty, lowercase and mixed-case variants. `trust_env=False` alone does not
disable Python's `SSLKEYLOGFILE`; Gemini constructs its explicit SSL context
only after this preflight, with no key-log filename. Existing owned clients
retain their explicit TLS/HTTP configuration if ambient settings appear later;
new OpenAI/Anthropic SDK copies repeat the admission guard.

Close OpenAI and Anthropic clients with their normal async context manager.
Gemini owns both clients: call `client.close()` and `await client.aio.aclose()`.
`MockTransport` supplies both transport protocols; a different sync Gemini
transport requires a separate `async_transport`. OpenAI and Anthropic use SDK subclasses
whose constructor guard also applies to
`copy()` and `with_options()`. Late ambient settings therefore refuse the copy
before SDK initialization; existing clients and caller-owned environments remain
unchanged. Clean copies retain the SDK's arguments, owned HTTP clients and wire
shape. New factories default to one HTTP attempt, including Gemini models and
interactions. Anthropic accepts an explicit `max_retries` and `timeout` for tests
exercising retry behavior.

The existing `build_fake_anthropic`, `build_stub_anthropic`,
`build_no_retry_anthropic` and `ScriptedTransport.client` inherit the isolation guard. Their test key, provider
URL, SDK timeout and retry behavior are preserved: fake/stub use two retries;
scripted defaults to zero and honors its explicit retry budget. The existing
`build_no_retry_anthropic` remains a separate builder outside this change.

## Owned database session factories

Call `daimon.testing.db.require_owned_sessionmaker(factory_or_engine)` before
opening a test-hook session. It validates the main bind and every routed
`binds` value, including connection binds' engines, without connecting or
changing the factory. Only local PostgreSQL `daimon_test_nc_*` lane databases
are allowed. The `daimon_test` exception requires both `GITHUB_ACTIONS=true`
and `CI=true`, plus `build_test_engine` provenance for a current-process
`test_w<pid>_<8hex>` or `test_f<pid>_<8hex>` schema on every engine. Execution
options and engine clones do not establish that provenance. Unbound factories,
foreign or synchronous binds, URL overrides and malformed routing maps fail
before I/O. Shared fixture setup also runs the preflight described above.

Offline admin proof fixtures skip a contributor's plain local `daimon_test`
outside exact GitHub Actions CI, with a clear lane-database/isolation reason.
The shared admission guard remains strict: invalid CI provenance and routed
foreign engines are errors, rather than skipped authorization failures.
```python
from daimon.testing.slack_surface import SlackSurfaceHarness

capture = await SlackSurfaceHarness(
    evidence_id="qa:turn:1",
    session_id=acknowledged_session,
    root_turn_id=acknowledged_root,
).run(invoke_real_turn)
```

Use `TeamsSurfaceHarness` for Teams. The invocation receives a real lifecycle;
call `post_initial()` as the host does, and then run the actual turn driver with
that lifecycle. The callback return is ignored. Harness options select the real
adapter's notification, table, identity, summary, and host-balance configuration.
Balance reads require an explicitly supplied sessionmaker and tenant.

Slack uses real `AsyncWebClient` methods to serialize Block Kit objects, then a
fake `api_call` accepts only owned `chat.postMessage`, `chat.update` and
`chat.delete` calls. Teams uses the actual SDK `MessageActivityInput` objects and
`TimedSender` through an injectable fake `TeamsSender`. Unknown operations,
foreign destinations and unowned edits fail closed. These boundaries never
fall through to a real platform client.

`PlatformSurfaceCapture` is an immutable JSON-roundtrippable receipt with
identity, monotonic window bounds, ordered post events and surviving messages.
Each event preserves the serialized request separately from the accepted
message payload. Edits retain message order, delete events retire messages, and
history remains unchanged. Receipts freeze when the invocation exits, including
when it raises or is cancelled. The harness accepts one invocation only.

`post_capture_complete` requires one observed adapter terminal hook, a normally
returned invocation, and no delivery failures. It does not assert successful
root completion: a real error card can finish a post window. Swallowed send,
control or retirement failures also make the window incomplete; partial posts
remain available. `text_capture_complete` additionally requires no opaque visual
in any observed state, including a draft later replaced by an answer.

Message observations keep `body_texts`, `card_texts`, `control_labels` and
`fallback_text` separate. Slack markdown blocks retain full displayed text even
when the notification fallback is clipped or escaped; Slack native table cells
are recorded in delivery order. Teams adaptive-card text and controls retain
SDK wire aliases, and plain answer edits remove the old card. Accessibility and
notification fallbacks must not be flattened as an extra displayed answer.
Images, file attachments and unsupported visual nodes leave incomplete text
coverage. Captured strings are posted text leaves, not browser pixels or OCR.

A QA consumer must independently match the receipt to acknowledged native
session/root and host window evidence before binding `VisibleText`. Preserve
transient event text for absence/preamble assertions; final surviving messages
alone cannot establish what was visible earlier. Control labels and card text
are visible leaves, but the receipt does not certify card finalization, tool or
approval effects, admission, platform ingress, authentication or live delivery.
No QA cell becomes PASS just because a harness returned a receipt.

Pinned OpenAI and Gemini provider replays use the shared isolated SDK factories,
with explicit offline transports and zero retries. Ambient provider settings
are refused before replay dispatch; injected clients do not inherit proxy or
TLS configuration. Request matching and source/version pins remain unchanged.

Catalog lifecycle wrappers still need the separately owned normalized-event
tap forwarding before native preview/draft completeness can be certified.
Keep those dependent checks PENDING; final lifecycle posts and native-root
completion receipts do not establish that additional coverage.
scripted defaults to zero and honors its explicit retry budget. The no-retry
builder accepts either a handler or `MARouter` and keeps zero retries, including
409/429/5xx errors. Its SDK copies retain that budget and the isolation guard.
## Read-only turn accounting

`daimon.testing.turn_accounting.TurnAccountingReader` reads Anthropic, OpenAI
and Gemini usage from an owned test database. Construct it with `sessions`, an
authorized tenant `scope`, the current `binding`, `native_session` and
`root_turn_id`. It checks every database bind before connecting and uses one
`REPEATABLE READ READ ONLY` snapshot. It neither claims outbox rows nor changes
usage, prices or ledger entries. The public store seam is
`daimon.core.stores.turn_usage.read_accounting_revisions(session, tenant_id=..., binding=...)`;
callers of that lower-level function own authorization and snapshot consistency.
The store suppresses autoflush without changing the caller's pending objects or
session policy; even an ordinary caller-owned transaction remains a read here.

```python
report = await reader.snapshot(
    attributions=tuple(revision_rates),  # RevisionRateAttribution per exact id/revision
    inventory=native_inventory,         # TurnUsageInventory captured independently
    markup=Decimal("1"),
)
```

Every `NormalizedUsageRevision` retains the original observation, native revision
and meter, latest/selected flags, optional settlement state, attributed model,
dated `ProviderPrice`, evidence references, token cost, infrastructure dollars
and seconds, total cost and signed ledger delta. A missing native model needs
independent admitted model context and `model_evidence_id`; a tariff alone does
not establish attribution. Missing rates, evidence, token stages or container
time remain unknown. `not_applicable` infrastructure requires explicit zero
dollars and an evidence reference; its duration stays `None`. Rates are supplied
explicitly because accounting revisions do not persist the applier's tariff.

`TurnUsageInventory` binds the binding/session/root to every latest native
id/revision and the evidence for a terminal inventory or exhausted request walk.
Capture it from the native path, never by enumerating these accounting rows.
It may attribute historical request spans lacking a persisted turn ID without
rewriting them. Missing members, stale revisions, other unattributed session
work, pending settlement and mismatched ledger cost prevent `complete=True`.
Disjoint child usage adds its own model work; independently measured shared
infrastructure must be attributed once. Covered aggregates and session-to-turn
allocations remain explicitly unsupported.

The report separates `coverage_complete`, `pricing_complete` and
`settlement_complete`. `total_usd` and `expected_ledger_usd` are `None` until
coverage and prices are complete; a known ledger debit can still be reported
while those totals remain unknown. `known_model_usd` is only the subtotal of
known selected model costs, including an empty subtotal of zero. All monetary
aggregation uses decimal precision 80. Controlled rates and inventory evidence
prove offline host arithmetic; they do not certify live invoices or supply a
missing posted footer. `issues` explain incomplete results.

## Metered channel budget proofs

`daimon.testing.channel_accounting.ChannelAccounting(sessions, scope, platform,
channel_id)` supplies an explicit owned-database accounting seam. Its
`snapshot(now=...)` reads channel ledger entries, usage projections, net spend
and the current production budget status in one repeatable read-only transaction.
All-time channel spend and the budget's windowed spend are separate. Every bind
must pass the shared owned/isolated-CI guard and use the same engine.

`await run_turn(deps=..., prepared=..., pricing=ControlledTurnPricing(...),
thread_id=..., external_user_id=..., user_message=..., lifecycle=..., cancel=...,
operation_key=..., binding=...)` drives the actual `run_prepared_turn` and turn
driver. Preparation, admission, isolated offline provider transports and actual
post capture belong to the caller. The seam binds the real legacy/provider usage
recorder to explicit **mock** prices instead of trusting the preparation's
recorder callback. It inserts no ledger or usage rows directly. Native providers
require their current durable binding and unchanged native usage; missing actual
infrastructure retains pending usage. It supports one root, without delegation
or recovery. The receipt retains the real host outcome, before/after independent
snapshots, new debit rows, pricing policy and explicit issues.
The success flag requires a completed single attempt; a failed or cancelled
turn can still carry genuine reconciled provider charges in its readback.
It correlates the run's observed meter with its exact persisted observation,
native root, revision and debit key. Another root's real charge or an ambiguous
native meter does not certify this run. Native revisions are included in the
same read-only snapshot, scoped through the channel's durable binding slots.

The proof uses USD 100 per million input/output tokens, zero explicitly
inapplicable offline native infrastructure and markup 1. A mocked 100-input /
50-output turn generates one genuine USD 0.015000 ledger debit through production
billing, exhausting the original USD 0.01 total channel budget. These exaggerated
rates exercise host arithmetic; they are not current provider prices or live
spend. Anthropic runs an isolated SDK HTTP/SSE fixture. OpenAI/Gemini accounting
proofs mock only the TurnIO boundary and use their real usage normalizers; this
does not certify their native codecs. The API alone proves neither first-turn
posted text nor the next admission refusal/no-send. Consumers retain those
independent NEW15 checks and do not promote source cells from a helper PASS.
## Host termination evidence

`daimon.testing.turn_conformance` exports `TerminationCase`,
`TerminationSnapshot`, `TerminationEvidence` and async `termination_evidence`.
Pass an owned `async_sessionmaker`, authorized `scope`, `platform`, an independent
`required_reasons` sequence and named `cases`. Each case declares `expected`,
`session` (or None before native creation), an async normal host `run` callback,
optional `expected_error_class` and `expected_recovered`. The callback returns
the actual `RunOutcome` from `run_prepared_turn` or terminal lifecycle `TurnState`.
For a raised recovery exception, retain the actual lifecycle state if testing
that specific reason; a bare exception only supplies its generic mapper fact.

The typed result contains the independent reason oracle and snapshots of actual
state/exception and committed reason, error class, session, account, tenant,
platform and recovery fields. The collector never seeds expected identity into
its observation or adopts a matching old outcome row. Missing committed rows
and unterminated states stay missing evidence. Every bind is checked with
`require_owned_sessionmaker` before execution. Provider owners must review complete
reachable-reason coverage and attach optional hooks explicitly. An absent hook
is PENDING; partial collector controls never certify C18 or a live provider.

## Opt-in product QA settings

`QAProductProfile("opt-in-product").flags` declares exactly six settings: core
`mcp.require_routine_destination`, `discord.credential_form_expiry`,
`discord.writers_none_notice`, `turn.native_footer_cost`, and each notebook/report
host's `html_link_errors`. Applying explicit copies combines settings in the same
section without consulting credentials or constructing settings. Missing product
declarations refuse with `ProductProfileUnavailable`; the report remains PENDING.
The default profile keeps all six disabled. An application receipt supplies no
source, native-send, card, accounting or user-visible outcome evidence.

The explicit `python -m daimon.testing.a7_anthropic --mode live` and
`python -m daimon.testing.f2_anthropic --mode live` entries require exact signed
financial authorization, a separate pinned lead GO, clean merged source,
installed config/ledger receipts, reviewed recipe and authorized disposable
bindings. Missing admission facts produce PENDING before credentials or a hold.
A7 runs one legacy and one mux turn in two fresh sessions sequentially. F2
uses the public default-capability flow with eleven authored skills, builtins
and an explicitly bound provider-reachable MCP endpoint; loopback fixtures
cannot supply live MCP evidence. Each session has one attempt, SDK retries zero,
and an exclusive envelope marker prevents a second run at a different output.

These are guard-only probes: the installed SDK has no native session budget
field and none is sent. Token thresholds are observed facts, never provider
caps. A7's total deadline is 300 seconds and streamed stop estimate $0.28; F2's
are 600 seconds and $0.52. Fifteen seconds within that same deadline are
reserved for interruption, archive and close. The stop uses the signed dated
Haiku 5.5 price table, disjoint model spans and a separately labelled wall-time
estimate. It cannot prevent unseen in-flight charges. Final missing runtime,
TTL splits or counters retain the whole $0.35/$0.65 hold for owner reconciliation;
no wall time or rounded native list cost is presented as exact billed spend.
Only acknowledged created IDs enter cleanup. Normalized F2 recordings use the
existing recorder; raw provider bodies and credentials are never exported.
The default modes remain offline and do not load credentials or a ledger.
