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
before I/O. Ordinary fixtures retain their existing behavior.

Guard adopters refuse plain local `daimon_test` by design. Their real-database
policy tests skip that documented contributor setup outside GitHub Actions CI;
use an owned lane database to exercise those tests locally.
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
ignored by the supplied clients.

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

The existing `build_fake_anthropic`, `build_stub_anthropic` and
`ScriptedTransport.client` inherit the isolation guard. Their test key, provider
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
before I/O. Ordinary fixtures retain their existing behavior.

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
