# Harness loop spikes (offline, not for merge)

This branch adds two opt-in engines behind the existing `ModelAdapter` seam to
measure what it takes to run the harness loop on something other than the
OpenAI Agents SDK. The service, journal, runtime, isolation, credentials and
the `daimon-harness/0` wire are unchanged; nothing selects these engines by
default, and `create_app` still builds `AzureModelAdapter`.

| engine | file | loop on | lines (all / code) |
|---|---|---|---|
| current | `model_adapter.py` | OpenAI Agents SDK 0.20.0 | 507 / 462 |
| `PydanticAIAdapter` | `pydantic_engine.py` | `pydantic-ai-slim` 1.107.9 | 619 / 513 |
| `DirectResponsesAdapter`, `DirectAnthropicAdapter` | `direct_engine.py` | `openai` and `anthropic` clients, no framework | 647 / 516 (about 110 for the Anthropic path) |

Both engines keep the shell's guarantees: a durable `model_request_started`
before model I/O and exactly one usage or `usage_unavailable` record per
request; `auto`/`ask`/`deny` policy including MCP `server/tool` names;
approvals bound to the arguments the model sent, decided once, in process;
zero retries at every layer; per-turn MCP over streamable HTTP with the bound
credential headers and no redirects; inline skills; tools under the
per-session UID; cancellation that never re-runs an interrupted tool.

## Results (offline, scripted models only)

- Full harness suite with both engines' tests:
  `pytest apps/harness/tests -n 2` gives 282 passed, 5 skipped (the baseline
  without them is 163 passed, 3 skipped). The skips are live-provider tests.
- Given the same scripted Azure Responses traffic, both new engines put
  the same requests on the wire as the current engine. The direct engine also
  produces identical history and events. The Pydantic engine differs only in
  minor fields (`status` on replayed calls, trimmed instructions, explicit
  `stream`/`tool_choice`). See `test_requests_and_events_match_the_agents_sdk_engine`
  and `test_history_and_requests_match_the_sdk_adapter`.
- The URL-driven harness conformance kit from draft PR #1261 (15 cases: approvals, cancel, restart
  without re-run, usage per request, skills, MCP, `tools=[]`) passed against
  the service running each engine.
- Pyright strict and Ruff are clean on the new files.

## What each engine needed

Pydantic AI:

- Two private hooks: an override of `OpenAIResponsesModel._process_response`
  to see the Responses `status`, `incomplete_details` and `error` (the public
  result drops them and maps missing usage to zero), and a capability that
  adds a rejection tool to `RunContext.tool_manager` so a call to a hidden
  `server/*` tool is refused like today instead of failing the run.
- A pinned model profile: the default profile for `gpt-6-luna` turns off
  encrypted reasoning content and rewrites MCP tool schemas.
- `retries=0`, `output_retries=0` and a raw-argument validator, so bad tool
  arguments come back as a tool result instead of a hidden extra request.
- History is stored as Pydantic AI messages, about 4 times larger than the
  current Responses items, and existing sessions cannot be read.
- The 1.x line installs against the current lock. 2.59.0 requires
  `openai>=3.26` and `anthropic>=1.12.1`, i.e. a repository-wide SDK upgrade;
  1.x gets security fixes for at least six months after 2.0 per the upstream
  version policy.

Hand-written loop:

- About 40 lines of loop proper (`Turn.converse`, `request`, `call_tool`);
  the rest is provider translation, metering, MCP and overflow handling.
- History stays in today's Responses format, so sessions remain compatible.
- A second provider (Anthropic Messages) cost about 110 lines; Anthropic
  usage excludes cached tokens and thinking tokens are not reported separately.

Both engines had to close MCP transports outside the exception path: the
`mcp` client's task groups otherwise wrap `HarnessError` in an
`ExceptionGroup` and the service loses the error kind.

## Upgrade note for the current engine

With `openai-agents` 0.24.0 and `openai` 3.35.1 the unchanged harness tests
give 11 failures (non-completed response mapping and SDK debug-log filters),
because `MeteredResponsesModel` overrides the SDK's private `_fetch_response`.

## Not covered

No live model call was made. Azure, Anthropic, image build, context-window
recovery against a real provider and the pending checkpoint work are
unverified for these engines. Engine selection needs a small `service.py`
change (`create_app` default and the `isinstance(..., AzureModelAdapter)`
credential close).

## Running the spike tests

The Pydantic tests skip unless `pydantic_ai` is importable. To run them
without touching the root lock, sync the harness into a separate environment
and add the package:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/harness-spike uv sync --frozen --package daimon-harness --group dev
uv pip install --python /tmp/harness-spike/bin/python 'pydantic-ai-slim[openai,anthropic]==1.107.9'
PYDANTIC_AI_NO_BANNER=1 /tmp/harness-spike/bin/python -m pytest apps/harness/tests -n 2
```
