"""Pydantic AI engine for the harness loop (spike, opt-in).

The shell (``TurnContext``, the supervisor and the journal) keeps owning policy, approvals,
tool events, usage records, sandboxing and cancellation. This module only translates between
that contract and Pydantic AI 1.107.9: a metering ``WrapperModel``, a raw-argument toolset and
the glue to run ``Agent.run`` once (twice after a context overflow).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from typing import Any, cast
from uuid import uuid4

from azure.identity.aio import get_bearer_token_provider
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool as McpTool
from mux.drivers.selfhosted.wire import MCPServer
from openai import AsyncAzureOpenAI, BadRequestError
from openai.types.responses import Response
from pydantic import JsonValue, ValidationError
from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ImageUrl,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.usage import UsageLimits

from .credentials import mcp_headers, remember_log_secret
from .errors import HarnessError
from .model_adapter import JSON, AzureModelAdapter, bound_mcp_http_client
from .runtime import TurnContext

RAW_ARGUMENTS = "arguments"
OVERFLOW_CODES = {"context_length_exceeded", "context_window_exceeded"}
TOOL_OUTPUT_LIMIT = 8192
# The Agents SDK engine builds its MCP ClientSession with a 5 second read timeout.
MCP_REQUEST_TIMEOUT = timedelta(seconds=5)
AZURE_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"

# The deployment name is arbitrary, so profile inference from it is useless. Pin what the
# stateless Azure Responses contract needs: encrypted reasoning round trips (this also turns
# on the include flag and item ids) and message phases. No schema transformer, so tool
# schemas reach the model exactly as registered.
AZURE_PROFILE = OpenAIModelProfile(
    openai_supports_encrypted_reasoning_content=True,
    openai_supports_phase=True,
    json_schema_transformer=None,
)
MODEL_SETTINGS = OpenAIResponsesModelSettings(parallel_tool_calls=False, openai_store=False)


# --- Model: raw response capture and metering ------------------------------------------


@dataclass(frozen=True)
class RawResponse:
    """What ``ModelResponse`` drops: provider status, error and whether usage was reported."""

    status: str | None
    incomplete_reason: str | None
    error: str | None
    usage_reported: bool

    def failure(self) -> HarnessError | None:
        if self.status == "completed":
            return None
        if self.status == "incomplete":
            return HarnessError(
                "model_incomplete",
                "Model response incomplete: " + (self.incomplete_reason or "unknown"),
            )
        return HarnessError(
            "model_failed", f"Model response {self.status or 'unknown'}: {self.error or 'unknown'}"
        )


class CapturingResponsesModel(OpenAIResponsesModel):
    """1.107.9 maps ``status``/``incomplete_details`` into ``provider_details['finish_reason']``
    and loses ``error`` and usage presence, so keep the raw response outcome here."""

    raw: RawResponse | None = None

    def _process_response(
        self,
        response: Response,
        model_settings: OpenAIResponsesModelSettings,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        details = response.incomplete_details
        error = response.error
        self.raw = RawResponse(
            status=response.status,
            incomplete_reason=details.reason if details else None,
            error=f"{error.code}: {error.message}" if error else None,
            usage_reported=response.usage is not None,
        )
        return super()._process_response(response, model_settings, model_request_parameters)


def policy_name(context: TurnContext, name: str) -> str:
    """``server__tool`` becomes ``server/tool``; the longest matching server name wins."""
    for server in sorted(context.request.mcp_servers, key=lambda s: len(s.name), reverse=True):
        prefix = server.name + "__"
        if name.startswith(prefix):
            return server.name + "/" + name[len(prefix) :]
    return name


class MeteredModel(WrapperModel):
    """One ``model_request_started`` before I/O and exactly one usage or unavailable record."""

    def __init__(self, wrapped: CapturingResponsesModel, context: TurnContext) -> None:
        super().__init__(wrapped)
        self.capture = wrapped
        self.context = context

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        context = self.context
        context.model_input = dump_messages(messages)
        request_id = str(uuid4())
        await context.emit(
            "model_request_started", {"request_id": request_id, "model": context.request.model}
        )
        self.capture.raw = None
        visible = replace(
            model_request_parameters,
            function_tools=[
                tool
                for tool in model_request_parameters.function_tools
                if context.permission(policy_name(context, tool.name)) != "deny"
            ],
        )
        try:
            response = await self.wrapped.request(messages, model_settings, visible)
        except (Exception, asyncio.CancelledError) as exc:
            await self.unavailable(request_id, "Model request ended without usage")
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HarnessError("usage_unavailable", "Model request ended without usage") from exc
        raw = self.capture.raw
        if raw is None:  # e.g. an Azure content-filter 400 that Pydantic AI turned into a response
            await self.unavailable(request_id, "Model request ended without usage")
            raise HarnessError("usage_unavailable", "Model request ended without usage")
        if not raw.usage_reported or not response.provider_response_id:
            await self.unavailable(request_id, "Model response did not report usage")
            raise HarnessError("usage_missing", "Model response did not report usage")
        usage = response.usage
        await context.emit(
            "usage",
            {
                "request_id": request_id,
                "model": context.request.model,
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.cache_read_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.details.get("reasoning_tokens", 0),
            },
        )
        for message_id, text in assistant_messages(response):
            await context.emit("agent_message_delta", {"message_id": message_id, "text": text})
            await context.emit(
                "agent_message",
                {"message_id": message_id, "parts": [{"type": "text", "text": text}]},
            )
        failure = raw.failure()
        if failure is not None:
            raise failure
        return response

    async def unavailable(self, request_id: str, message: str) -> None:
        await self.context.emit(
            "error",
            {
                "kind": "usage_unavailable",
                "message": message,
                "retryable": False,
                "request_id": request_id,
            },
        )


def assistant_messages(response: ModelResponse) -> list[tuple[str, str]]:
    """One ``(message_id, text)`` per Responses message item; its TextParts share an id."""
    grouped: list[tuple[str | None, list[str]]] = []
    for part in response.parts:
        if isinstance(part, TextPart):
            if grouped and part.id is not None and grouped[-1][0] == part.id:
                grouped[-1][1].append(part.content)
            else:
                grouped.append((part.id, [part.content]))
    return [(str(uuid4()), "".join(texts)) for _, texts in grouped]


# --- History -----------------------------------------------------------------------------


def dump_messages(messages: list[ModelMessage]) -> list[JsonValue]:
    return cast(list[JsonValue], ModelMessagesTypeAdapter.dump_python(messages, mode="json"))


def persistable(messages: list[ModelMessage]) -> list[JsonValue]:
    """Instructions are re-sent on every run; storing them per request would copy the skills."""
    stripped = [
        replace(message, instructions=None) if isinstance(message, ModelRequest) else message
        for message in messages
    ]
    return dump_messages(stripped)


def load_history(history: list[JsonValue]) -> list[ModelMessage]:
    """Pydantic AI messages, plus the plain user items the supervisor and journal inject."""
    messages: list[ModelMessage] = []
    for item in history:
        if isinstance(item, dict) and item.get("kind") in {"request", "response"}:
            messages.extend(ModelMessagesTypeAdapter.validate_python([item]))
        elif (
            isinstance(item, dict)
            and item.get("role") == "user"
            and isinstance(item.get("content"), str)
        ):
            messages.append(ModelRequest(parts=[UserPromptPart(content=str(item["content"]))]))
        else:
            raise HarnessError("history_unsupported", "History item is not a Pydantic AI message")
    return messages


def user_content(context: TurnContext) -> list[UserContent]:
    content: list[UserContent] = []
    for part in context.request.input:
        if part.type == "text":
            content.append(part.text)
        else:  # the data URL is forwarded verbatim, with the same ``detail="auto"``
            content.append(ImageUrl(url=f"data:{part.media_type};base64,{part.data_b64}"))
    return content


def turn_messages(messages: list[ModelMessage], prompt: UserPromptPart) -> list[ModelMessage]:
    """The current turn only. History merging can fold the prompt into an older request."""
    for index, message in enumerate(messages):
        if isinstance(message, ModelRequest):
            for position, part in enumerate(message.parts):
                if part is prompt:
                    first = replace(message, parts=message.parts[position:])
                    return [first, *messages[index + 1 :]]
    raise HarnessError("history_lost", "Current turn input is missing from the request")


def compact_outputs(messages: list[ModelMessage]) -> list[ModelMessage]:
    compacted: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            parts = [
                replace(
                    part,
                    content=part.content[:TOOL_OUTPUT_LIMIT] + "\n[output truncated for context]",
                )
                if isinstance(part, ToolReturnPart)
                and isinstance(part.content, str)
                and len(part.content) > TOOL_OUTPUT_LIMIT
                else part
                for part in message.parts
            ]
            message = replace(message, parts=parts)
        compacted.append(message)
    return compacted


def is_context_overflow(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, BadRequestError) and exc.code in OVERFLOW_CODES:
            return True
        exc = exc.__cause__
    return False


# --- Tools -------------------------------------------------------------------------------


class RawArguments:
    """Args validator that never parses: the shell must see exactly what the model sent."""

    def validate_json(self, input: str | bytes | bytearray, **_: object) -> dict[str, str]:
        return {RAW_ARGUMENTS: input if isinstance(input, str) else bytes(input).decode()}

    def validate_python(self, input: object, **_: object) -> dict[str, str]:
        return {RAW_ARGUMENTS: input if isinstance(input, str) else json.dumps(input)}


@dataclass(frozen=True)
class ToolSpec:
    name: str  # what the model calls
    policy: str  # what tool policy and tool events call it
    description: str
    schema: dict[str, Any]
    strict: bool
    mcp: bool
    run: Callable[[dict[str, JsonValue]], Awaitable[JsonValue]]


BUILTIN_FIELDS: dict[str, dict[str, dict[str, str]]] = {
    "bash": {"command": {"type": "string"}},
    "read": {"path": {"type": "string"}},
    "write": {"path": {"type": "string"}, "content": {"type": "string"}},
    "edit": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
}


def builtin_specs(context: TurnContext) -> list[ToolSpec]:
    async def run(tool: str, args: dict[str, JsonValue]) -> JsonValue:
        if tool == "bash":
            return await context.bash(str(args["command"]))
        return await context.async_file_operation(
            tool,
            str(args["path"]),
            content=str(args.get("content", "")),
            old=str(args.get("old", "")),
            new=str(args.get("new", "")),
        )

    return [
        ToolSpec(
            name=name,
            policy=name,
            description=f"{name} in the persistent workspace",
            schema={
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
            strict=True,
            mcp=False,
            run=partial(run, name),
        )
        for name, fields in BUILTIN_FIELDS.items()
    ]


def mcp_specs(server: str, session: ClientSession, tools: list[McpTool]) -> list[ToolSpec]:
    def make(remote: McpTool) -> ToolSpec:
        async def run(args: dict[str, JsonValue]) -> JsonValue:
            result = await session.call_tool(
                remote.name, args, read_timeout_seconds=MCP_REQUEST_TIMEOUT
            )
            if result.isError:
                raise ValueError("MCP tool returned an error")
            return JSON.validate_python(result.model_dump(mode="json"))

        return ToolSpec(
            name=f"{server}__{remote.name}",
            policy=f"{server}/{remote.name}",
            description=remote.description or "MCP tool",
            schema=remote.inputSchema,
            strict=False,
            mcp=True,
            run=run,
        )

    return [make(remote) for remote in tools]


async def never_execute(_: dict[str, JsonValue]) -> JsonValue:
    raise AssertionError("denied tool must not execute")


class HarnessToolset(AbstractToolset[TurnContext]):
    """Every tool call goes through ``TurnContext.tool``; Pydantic AI never validates arguments.

    Denied tools stay registered so a call to one reaches the shell and is refused there. The
    metering model hides them from the model.
    """

    def __init__(self, context: TurnContext, specs: list[ToolSpec]) -> None:
        self.context = context
        self.specs = {spec.name: spec for spec in specs}

    @property
    def id(self) -> str | None:
        return "harness"

    def tool(self, spec: ToolSpec) -> ToolsetTool[TurnContext]:
        return ToolsetTool(
            toolset=self,
            tool_def=ToolDefinition(
                name=spec.name,
                description=spec.description,
                parameters_json_schema=spec.schema,
                strict=spec.strict,
            ),
            max_retries=0,
            args_validator=RawArguments(),
        )

    def reject(self, name: str) -> bool:
        """Register a rejection for a name the model was never offered but the policy denies."""
        policy = policy_name(self.context, name)
        if name in self.specs or self.context.permission(policy) != "deny":
            return False
        self.specs[name] = ToolSpec(
            name, policy, "Local policy rejection", {}, False, "/" in policy, never_execute
        )
        return True

    async def get_tools(self, ctx: RunContext[TurnContext]) -> dict[str, ToolsetTool[TurnContext]]:
        return {name: self.tool(spec) for name, spec in self.specs.items()}

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[TurnContext],
        tool: ToolsetTool[TurnContext],
    ) -> str:
        spec = self.specs[name]
        raw = cast(str, tool_args[RAW_ARGUMENTS])
        assert ctx.tool_call_id is not None
        if self.context.permission(spec.policy) == "deny":
            arguments: dict[str, JsonValue] = {"arguments": raw}
            run = never_execute
        else:
            try:
                parsed = JSON.validate_json(raw)
            except ValidationError as exc:
                return tool_error(str(exc))
            if not isinstance(parsed, dict):
                return tool_error("Tool arguments must be a JSON object")
            arguments, run = parsed, spec.run
        output = await self.context.tool(
            ctx.tool_call_id, spec.policy, arguments, partial(run, arguments), mcp=spec.mcp
        )
        return json.dumps(output)


def tool_error(message: str) -> str:
    """Mirrors the Agents SDK default: invalid arguments are a model-visible tool result."""
    return f"An error occurred while running the tool. Please try again. Error: {message}"


class RejectDeniedCalls(AbstractCapability[TurnContext]):
    """The toolset cannot list names it has never seen (``server/*: deny``), and Pydantic AI
    treats an unlisted name as a failed call. Before the response is processed, add a tool for
    such a name to the step's ToolManager, cloned from one of ours (mutates ``tools``)."""

    def __init__(self, toolset: HarnessToolset) -> None:
        self.toolset = toolset

    async def after_model_request(
        self,
        ctx: RunContext[TurnContext],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        manager = ctx.tool_manager
        assert manager is not None and manager.tools is not None
        template = next(tool for tool in manager.tools.values() if tool.toolset is self.toolset)
        for part in response.parts:
            if (
                isinstance(part, ToolCallPart)
                and part.tool_name not in manager.tools
                and self.toolset.reject(part.tool_name)
            ):
                definition = replace(template.tool_def, name=part.tool_name)
                manager.tools[part.tool_name] = replace(template, tool_def=definition)
        return response


# --- MCP ---------------------------------------------------------------------------------


@asynccontextmanager
async def mcp_session(url: str, headers: dict[str, str]) -> AsyncIterator[ClientSession]:
    """The ``mcp`` client with the shell's no-redirect httpx client. Pydantic AI's own
    ``MCPServerStreamableHTTP`` accepts ``http_client=`` (mutually exclusive with ``headers``)
    but turns MCP errors into model retries, so it is not used."""
    async with (
        bound_mcp_http_client(headers=headers) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _),
        ClientSession(read, write, MCP_REQUEST_TIMEOUT) as session,
    ):
        await session.initialize()
        yield session


def first_leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup):
        error = cast(BaseExceptionGroup[BaseException], error).exceptions[0]
    return error


async def connect_mcp(
    sessions: AsyncExitStack, context: TurnContext, config: MCPServer
) -> list[ToolSpec]:
    headers = mcp_headers(config, context.credentials, context.credential_bindings)
    try:
        session = await sessions.enter_async_context(mcp_session(config.url, headers))
        return mcp_specs(config.name, session, (await session.list_tools()).tools)
    except BaseExceptionGroup as group:  # anyio task groups inside the MCP client wrap failures
        raise first_leaf(group) from None


# --- Engine ------------------------------------------------------------------------------


class PydanticAIAdapter(AzureModelAdapter):
    """``AzureModelAdapter`` supplies credentials, redaction and ``run``; this swaps the loop."""

    async def _run(
        self, context: TurnContext, history: list[JsonValue], log_credentials: dict[str, str]
    ) -> list[JsonValue]:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(self.open_client(log_credentials))
            # Closed without the in-flight exception: anyio task groups inside the MCP client
            # would otherwise wrap a HarnessError in an ExceptionGroup on its way out.
            sessions = AsyncExitStack()
            stack.push_async_callback(sessions.aclose)
            specs = builtin_specs(context)
            for config in context.request.mcp_servers:
                specs += await connect_mcp(sessions, context, config)
            toolset = HarnessToolset(context, specs)
            model = MeteredModel(
                CapturingResponsesModel(
                    context.request.model,
                    provider=AzureProvider(openai_client=client),
                    profile=AZURE_PROFILE,
                ),
                context,
            )
            agent = Agent[TurnContext, str](
                model,
                name="Daimon",
                instructions=context.request.instructions + "\n\n" + await context.skills(),
                toolsets=[toolset],
                capabilities=[RejectDeniedCalls(toolset)],
                model_settings=MODEL_SETTINGS,
                retries=0,
                deps_type=TurnContext,
            )
            prompt = UserPromptPart(content=user_content(context))
            messages = [*load_history(history), ModelRequest(parts=[prompt])]
            limits = UsageLimits(request_limit=context.request.limits.max_tool_calls + 2)
            for attempt in range(2):
                with capture_run_messages() as sent:
                    try:
                        result = await agent.run(
                            message_history=messages,
                            deps=context,
                            usage_limits=limits,
                            infer_name=False,
                        )
                        return persistable(result.all_messages())
                    except Exception as exc:
                        if attempt == 1 or not is_context_overflow(exc):
                            raise
                await context.emit(
                    "error",
                    {
                        "kind": "context_truncated",
                        "message": "Context limit reached; older history was removed",
                        "retryable": True,
                    },
                )
                # Keep reasoning and paired calls/results of this turn so the stateless retry
                # stays valid and cannot repeat completed side effects.
                messages = compact_outputs(turn_messages(sent, prompt))
            raise AssertionError("context recovery exhausted")

    def open_client(self, log_credentials: dict[str, str]) -> AsyncAzureOpenAI:
        """The Azure client exactly as ``AzureModelAdapter._run`` builds it (that code is
        inline there, so it is repeated here)."""
        token_provider: Callable[[], Awaitable[str]] | None = None
        if not self.key:
            assert self.credential is not None
            azure_token = get_bearer_token_provider(self.credential, AZURE_TOKEN_SCOPE)

            async def azure_bearer_token() -> str:
                token = await azure_token()
                remember_log_secret(log_credentials, "azure_bearer", token)
                return token

            token_provider = azure_bearer_token
        return AsyncAzureOpenAI(
            azure_endpoint=self.endpoint,
            api_version=self.api_version,
            api_key=self.key,
            azure_ad_token_provider=token_provider,
            max_retries=0,
        )
