"""A minimal agent loop written directly over the model APIs, with no agent framework.

Only the loop is new. Approvals, tool policy, durable events, usage records, tool
sandboxing and cancellation stay in `TurnContext` and the service, exactly as they
are for `AzureModelAdapter`. Two providers sit behind the small `Provider` interface:
Azure OpenAI Responses (the default engine) and Anthropic Messages.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import partial
from typing import Protocol, cast
from uuid import uuid4

from anthropic import AsyncAnthropic
from anthropic import BadRequestError as AnthropicBadRequestError
from anthropic import omit as anthropic_omit
from anthropic.types import Message, MessageParam
from azure.identity.aio import get_bearer_token_provider
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mux.drivers.selfhosted.wire import MCPServer, Part
from openai import AsyncAzureOpenAI
from openai import BadRequestError as OpenAIBadRequestError
from openai.types.responses import FunctionToolParam, Response, ResponseInputParam
from pydantic import JsonValue, TypeAdapter

from .credentials import (
    credential_log_redaction,
    mcp_headers,
    process_credentials,
    remember_log_secret,
)
from .errors import HarnessError
from .isolation import validate_credential_sources
from .model_adapter import AzureModelAdapter, bound_mcp_http_client
from .runtime import TurnContext

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
TRIM_OUTPUT_CHARS = 8192  # tool outputs kept whole when overflow recovery compacts the context
ANTHROPIC_MAX_TOKENS = 16_000  # non-streaming requests above ~21k tokens are refused by the SDK

# ---------------------------------------------------------------------------
# Provider-neutral types: what the loop knows about a model, a tool and a response.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str  # as the model sees it ("write", "sample__echo")
    policy_name: str  # as tool_policy and the journal see it ("write", "sample/echo")
    description: str
    schema: dict[str, object]
    strict: bool
    mcp: bool
    run: Callable[[dict[str, JsonValue]], Awaitable[JsonValue]]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it


@dataclass(frozen=True)
class ToolOutput:
    call_id: str
    text: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int  # includes cached tokens
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int


@dataclass(frozen=True)
class Completion:
    items: list[JsonValue]  # provider-native conversation items to append, in order
    texts: list[str]  # assistant message texts, in order
    calls: list[ToolCall]
    usage: Usage | None
    failure: HarnessError | None  # the response did not complete


class Provider(Protocol):
    """Everything the loop needs from one model API. History items are provider-native."""

    def user_message(self, parts: list[Part]) -> JsonValue: ...

    def tool_results(self, outputs: list[ToolOutput]) -> list[JsonValue]: ...

    async def complete(
        self, instructions: str, items: list[JsonValue], tools: list[Tool]
    ) -> Completion: ...

    def is_context_overflow(self, exc: BaseException | None) -> bool: ...

    def trim_tool_outputs(self, items: list[JsonValue]) -> list[JsonValue]: ...


# ---------------------------------------------------------------------------
# Loop core, metering and error mapping.
# ---------------------------------------------------------------------------


class Turn:
    """One harness turn: call the model, run its tool calls, repeat until it answers."""

    def __init__(self, context: TurnContext, provider: Provider, tools: list[Tool]) -> None:
        self.context = context
        self.provider = provider
        self.tools = {tool.name: tool for tool in tools}

    async def run(self, history: list[JsonValue]) -> list[JsonValue]:
        context = self.context
        instructions = context.request.instructions + "\n\n" + await context.skills()
        items = [*history, self.provider.user_message(context.request.input)]
        try:
            return await self.converse(instructions, items)
        except HarnessError as exc:
            if exc.kind != "usage_unavailable" or not self.provider.is_context_overflow(
                exc.__cause__
            ):
                raise
        # Retry once without the old history. The current turn's reasoning, calls and
        # results stay together, so the stateless retry is valid and repeats no side effect.
        await context.emit(
            "error",
            {
                "kind": "context_truncated",
                "message": "Context limit reached; older history was removed",
                "retryable": True,
            },
        )
        recent = context.model_input[len(history) :]
        return await self.converse(instructions, self.provider.trim_tool_outputs(recent))

    async def converse(self, instructions: str, items: list[JsonValue]) -> list[JsonValue]:
        items = list(items)
        for _ in range(self.context.request.limits.max_tool_calls + 2):
            completion = await self.request(instructions, items)
            items += completion.items
            if not completion.calls:
                return items
            outputs = [await self.call_tool(call) for call in completion.calls]
            items += self.provider.tool_results(outputs)
        raise HarnessError("max_turns", "Maximum model requests exceeded")

    async def request(self, instructions: str, items: list[JsonValue]) -> Completion:
        """One metered model request: exactly one usage event or one usage_unavailable."""
        context = self.context
        context.model_input = list(items)
        request_id = str(uuid4())
        await context.emit(
            "model_request_started", {"request_id": request_id, "model": context.request.model}
        )
        try:
            completion = await self.provider.complete(
                instructions, items, list(self.tools.values())
            )
        except (Exception, asyncio.CancelledError) as exc:
            await self.unmeasured(request_id, "Model request ended without usage")
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HarnessError("usage_unavailable", "Model request ended without usage") from exc
        usage = completion.usage
        if usage is None:
            await self.unmeasured(request_id, "Model response did not report usage")
            raise HarnessError("usage_missing", "Model response did not report usage")
        await context.emit(
            "usage",
            {
                "request_id": request_id,
                "model": context.request.model,
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            },
        )
        for text in completion.texts:
            message_id = str(uuid4())
            await context.emit("agent_message_delta", {"message_id": message_id, "text": text})
            await context.emit(
                "agent_message",
                {"message_id": message_id, "parts": [{"type": "text", "text": text}]},
            )
        if completion.failure is not None:
            raise completion.failure  # after usage is recorded; its tool calls never run
        return completion

    async def unmeasured(self, request_id: str, message: str) -> None:
        await self.context.emit(
            "error",
            {
                "kind": "usage_unavailable",
                "message": message,
                "retryable": False,
                "request_id": request_id,
            },
        )

    async def call_tool(self, call: ToolCall) -> ToolOutput:
        context = self.context
        tool = self.tools.get(call.name)
        if tool is None:
            # Hidden by policy (or invented by the model): keep the call in the record and
            # refuse it without executing anything.
            policy = policy_name(context.request.mcp_servers, call.name)
            if context.permission(policy) != "deny":
                return ToolOutput(call.call_id, f"Unknown tool: {call.name}")
            output = await context.tool(
                call.call_id,
                policy,
                {"arguments": call.arguments},
                never_execute,
                mcp="/" in policy,
            )
            return ToolOutput(call.call_id, json.dumps(output))
        try:
            arguments = JSON.validate_json(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("not an object")
        except ValueError:
            return ToolOutput(call.call_id, "Invalid tool arguments: expected a JSON object")
        output = await context.tool(
            call.call_id,
            tool.policy_name,
            arguments,
            lambda: tool.run(arguments),
            mcp=tool.mcp,
        )
        return ToolOutput(call.call_id, json.dumps(output))


async def never_execute() -> JsonValue:
    raise AssertionError("denied tool must not execute")


async def run_turn(
    context: TurnContext, history: list[JsonValue], provider: Provider
) -> list[JsonValue]:
    """Register tools (MCP connections live for exactly this turn), then run the loop."""
    stack = AsyncExitStack()
    try:
        tools = builtin_tools(context)
        for config in context.request.mcp_servers:
            tools += await mcp_tools(stack, context, config)
        return await Turn(context, provider, tools).run(history)
    finally:
        # Close the transports with no exception in flight. Their anyio task groups wrap
        # an exception passing through them in an ExceptionGroup, which would hide
        # HarnessError kinds (tool_limit, model_failed) from the service.
        await stack.aclose()


# ---------------------------------------------------------------------------
# Tool registration: built-in workspace tools and MCP tools over streamable HTTP.
# ---------------------------------------------------------------------------

BUILTIN_FIELDS: dict[str, dict[str, dict[str, str]]] = {
    "bash": {"command": {"type": "string"}},
    "read": {"path": {"type": "string"}},
    "write": {"path": {"type": "string"}, "content": {"type": "string"}},
    "edit": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
}


async def run_builtin(context: TurnContext, name: str, args: dict[str, JsonValue]) -> JsonValue:
    if name == "bash":
        return await context.bash(str(args["command"]))
    return await context.async_file_operation(
        name,
        str(args["path"]),
        content=str(args.get("content", "")),
        old=str(args.get("old", "")),
        new=str(args.get("new", "")),
    )


def builtin_tools(context: TurnContext) -> list[Tool]:
    return [
        Tool(
            name=name,
            policy_name=name,
            description=f"{name} in the persistent workspace",
            schema={
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
            strict=True,
            mcp=False,
            run=partial(run_builtin, context, name),
        )
        for name, fields in BUILTIN_FIELDS.items()
        if context.permission(name) != "deny"
    ]


async def mcp_tools(stack: AsyncExitStack, context: TurnContext, config: MCPServer) -> list[Tool]:
    headers = mcp_headers(config, context.credentials, context.credential_bindings)
    http = await stack.enter_async_context(bound_mcp_http_client(headers=headers))
    read, write, _ = await stack.enter_async_context(
        streamable_http_client(config.url, http_client=http)
    )
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    async def call(name: str, args: dict[str, JsonValue]) -> JsonValue:
        result = await session.call_tool(name, args)
        if result.isError:
            raise ValueError("MCP tool returned an error")
        return JSON.validate_python(result.model_dump(mode="json"))

    return [
        Tool(
            name=f"{config.name}__{remote.name}",
            policy_name=f"{config.name}/{remote.name}",
            description=remote.description or "MCP tool",
            schema=remote.inputSchema,
            strict=False,
            mcp=True,
            run=partial(call, remote.name),
        )
        for remote in (await session.list_tools()).tools
        if context.permission(f"{config.name}/{remote.name}") != "deny"
    ]


def policy_name(servers: list[MCPServer], name: str) -> str:
    """Map a model-facing `server__tool` name back to the `server/tool` policy key."""
    for server in sorted(servers, key=lambda config: len(config.name), reverse=True):
        prefix = server.name + "__"
        if name.startswith(prefix):
            return server.name + "/" + name[len(prefix) :]
    return name


# ---------------------------------------------------------------------------
# Provider: Azure OpenAI Responses. History items are Responses input items, so
# sessions written by the SDK-based adapter stay readable.
# ---------------------------------------------------------------------------


class ResponsesProvider:
    def __init__(self, client: AsyncAzureOpenAI, model: str) -> None:
        self.client = client
        self.model = model

    def user_message(self, parts: list[Part]) -> JsonValue:
        content: list[JsonValue] = [
            {"type": "input_text", "text": part.text}
            if part.type == "text"
            else {
                "type": "input_image",
                "image_url": f"data:{part.media_type};base64,{part.data_b64}",
                "detail": "auto",
            }
            for part in parts
        ]
        return {"role": "user", "content": content}

    def tool_results(self, outputs: list[ToolOutput]) -> list[JsonValue]:
        return [
            {"type": "function_call_output", "call_id": o.call_id, "output": o.text}
            for o in outputs
        ]

    async def complete(
        self, instructions: str, items: list[JsonValue], tools: list[Tool]
    ) -> Completion:
        response = await self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=cast(ResponseInputParam, items),
            tools=[
                FunctionToolParam(
                    type="function",
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.schema,
                    strict=tool.strict,
                )
                for tool in tools
            ],
            store=False,
            include=["reasoning.encrypted_content"],
            parallel_tool_calls=False,
        )
        return self.completion(response)

    def completion(self, response: Response) -> Completion:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for item in response.output:
            if item.type == "message":
                texts.append("".join(p.text for p in item.content if p.type == "output_text"))
            elif item.type == "function_call":
                calls.append(ToolCall(item.call_id, item.name, item.arguments))
        return Completion(
            # exclude_unset replays exactly what the API sent, including encrypted reasoning.
            items=[
                JSON.validate_python(item.model_dump(mode="json", exclude_unset=True))
                for item in response.output
            ],
            texts=texts,
            calls=calls,
            usage=responses_usage(response),
            failure=response_failure(response),
        )

    def is_context_overflow(self, exc: BaseException | None) -> bool:
        return isinstance(exc, OpenAIBadRequestError) and exc.code in {
            "context_length_exceeded",
            "context_window_exceeded",
        }

    def trim_tool_outputs(self, items: list[JsonValue]) -> list[JsonValue]:
        return [trim_output(item) for item in items]


def trim_output(item: JsonValue) -> JsonValue:
    if isinstance(item, dict) and item.get("type") == "function_call_output":
        output = item.get("output")
        if isinstance(output, str):
            return {**item, "output": trim(output)}
    return item


def responses_usage(response: Response) -> Usage | None:
    """Measured usage needs both integer token counts; anything less is unmeasured."""
    usage = response.usage
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    # type() rather than isinstance(): a JSON boolean is not a token count.
    if type(input_tokens) is not int or type(output_tokens) is not int:
        return None
    return Usage(
        input_tokens=input_tokens,
        cached_input_tokens=token_count(usage, "input_tokens_details", "cached_tokens"),
        output_tokens=output_tokens,
        reasoning_tokens=token_count(usage, "output_tokens_details", "reasoning_tokens"),
    )


def token_count(usage: object, details: str, field: str) -> int:
    """Read usage.<details>.<field>. The SDK does not validate responses, so absent keys raise."""
    value = getattr(getattr(usage, details, None), field, 0)
    return value if isinstance(value, int) else 0


def response_failure(response: Response) -> HarnessError | None:
    status = response.status or "unknown"
    if status == "completed":
        return None
    if status == "incomplete":
        details = response.incomplete_details
        reason = details.reason if details and details.reason else "unknown"
        return HarnessError("model_incomplete", "Model response incomplete: " + reason)
    error = response.error
    reason = f"{error.code}: {error.message}" if error else "unknown"
    return HarnessError("model_failed", f"Model response {status}: {reason}")


def trim(text: str) -> str:
    if len(text) <= TRIM_OUTPUT_CHARS:
        return text
    return text[:TRIM_OUTPUT_CHARS] + "\n[output truncated for context]"


# ---------------------------------------------------------------------------
# Provider: Anthropic Messages. History items are Messages API messages, so an
# Anthropic session is not interchangeable with a Responses session.
# ---------------------------------------------------------------------------


class MessagesProvider:
    def __init__(self, client: AsyncAnthropic, model: str) -> None:
        self.client = client
        self.model = model

    def user_message(self, parts: list[Part]) -> JsonValue:
        content: list[JsonValue] = [
            {"type": "text", "text": part.text}
            if part.type == "text"
            else {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": part.data_b64,
                },
            }
            for part in parts
        ]
        return {"role": "user", "content": content}

    def tool_results(self, outputs: list[ToolOutput]) -> list[JsonValue]:
        # All results answering one assistant message share a single user message.
        blocks: list[JsonValue] = [
            {"type": "tool_result", "tool_use_id": o.call_id, "content": o.text} for o in outputs
        ]
        return [{"role": "user", "content": blocks}]

    async def complete(
        self, instructions: str, items: list[JsonValue], tools: list[Tool]
    ) -> Completion:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=instructions,
            messages=cast(list[MessageParam], items),
            tools=[
                {"name": tool.name, "description": tool.description, "input_schema": tool.schema}
                for tool in tools
            ]
            or anthropic_omit,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True}
            if tools
            else anthropic_omit,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
        )
        return self.completion(message)

    def completion(self, message: Message) -> Completion:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in message.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, json.dumps(block.input)))
        usage = message.usage
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        # Thinking blocks carry signatures and must come back verbatim with their tool_use.
        content = [block.model_dump(mode="json", exclude_none=True) for block in message.content]
        return Completion(
            items=[JSON.validate_python({"role": "assistant", "content": content})],
            texts=texts,
            calls=calls,
            # Messages usage excludes cached tokens; the journal's input_tokens includes them.
            # Thinking tokens are folded into output_tokens, so reasoning_tokens is unknown (0).
            usage=Usage(
                input_tokens=usage.input_tokens + cache_read + cache_write,
                cached_input_tokens=cache_read,
                output_tokens=usage.output_tokens,
                reasoning_tokens=0,
            ),
            failure=stop_failure(message.stop_reason),
        )

    def is_context_overflow(self, exc: BaseException | None) -> bool:
        return isinstance(exc, AnthropicBadRequestError) and "prompt is too long" in exc.message

    def trim_tool_outputs(self, items: list[JsonValue]) -> list[JsonValue]:
        return [trim_message(item) for item in items]


def trim_message(item: JsonValue) -> JsonValue:
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, list):
            return {**item, "content": [trim_result(block) for block in content]}
    return item


def trim_result(block: JsonValue) -> JsonValue:
    if isinstance(block, dict):
        content = block.get("content")
        if block.get("type") == "tool_result" and isinstance(content, str):
            return {**block, "content": trim(content)}
    return block


def stop_failure(stop_reason: str | None) -> HarnessError | None:
    if stop_reason in {"end_turn", "tool_use", "stop_sequence"}:
        return None
    if stop_reason == "max_tokens":
        return HarnessError("model_incomplete", "Model response incomplete: max_tokens")
    return HarnessError("model_failed", f"Model response {stop_reason or 'unknown'}: unknown")


# ---------------------------------------------------------------------------
# Adapters: client construction and credentials only.
# ---------------------------------------------------------------------------


class DirectResponsesAdapter:
    """ModelAdapter over Azure OpenAI Responses with the hand-written loop."""

    def __init__(self) -> None:
        # Reuses the credential validation and managed-identity setup of the SDK adapter.
        self.azure = AzureModelAdapter()

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        with credential_log_redaction(
            self.azure.log_credentials | context.credentials
        ) as log_credentials:
            async with AsyncAzureOpenAI(
                azure_endpoint=self.azure.endpoint,
                api_version=self.azure.api_version,
                api_key=self.azure.key,
                azure_ad_token_provider=self.token_provider(log_credentials),
                max_retries=0,
            ) as client:
                return await run_turn(
                    context, history, ResponsesProvider(client, context.request.model)
                )

    def token_provider(
        self, log_credentials: dict[str, str]
    ) -> Callable[[], Awaitable[str]] | None:
        credential = self.azure.credential
        if credential is None:
            return None
        provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )

        async def bearer_token() -> str:
            token = await provider()
            remember_log_secret(log_credentials, "azure_bearer", token)
            return token

        return bearer_token


class DirectAnthropicAdapter:
    """ModelAdapter over Anthropic Messages with the same loop."""

    def __init__(self) -> None:
        validate_credential_sources()
        self.log_credentials = process_credentials()
        self.key = os.environ.get("ANTHROPIC_API_KEY")

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        with credential_log_redaction(self.log_credentials | context.credentials):
            async with AsyncAnthropic(api_key=self.key, max_retries=0) as client:
                return await run_turn(
                    context, history, MessagesProvider(client, context.request.model)
                )
