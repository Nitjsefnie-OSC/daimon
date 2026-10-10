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
Gemini additionally refuses every `GOOGLE_`/`GEMINI_` variable name (case
insensitive), including empty or unknown future settings; unknown names and all
values are omitted from errors. This includes provider keys, webhook/admin keys,
org/project settings, custom
headers, base URLs, Anthropic identity/configuration and Gemini credential,
Vertex and replay settings. Errors name variables without exposing values. The
factories never clear the process environment; explicit test keys prevent
credential-chain fallback. Start tests in a clean environment or remove only
the relevant variables with a scoped test fixture. Missing keys, routes or transports also fail before HTTP
client construction. Generic proxy and certificate environment settings are
ignored by the supplied clients.

Close OpenAI and Anthropic clients with their normal async context manager.
Gemini owns both clients: call `client.close()` and `await client.aio.aclose()`.
`MockTransport` supplies both transport protocols; a different sync Gemini
transport requires a separate `async_transport`. New factories default to one
HTTP attempt, including Gemini models and interactions. Anthropic accepts an
explicit `max_retries` and `timeout` for tests exercising retry behavior.

The existing `build_fake_anthropic`, `build_stub_anthropic` and
`ScriptedTransport.client` inherit the isolation guard. Their test key, provider
URL, SDK timeout and retry behavior are preserved: fake/stub use two retries;
scripted defaults to zero and honors its explicit retry budget. The existing
`build_no_retry_anthropic` remains a separate builder outside this change.
