"""Private SDK boundary. Construction and importing this module perform no I/O."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai._interactions import APIConnectionError, APIError, APIStatusError
from pydantic import JsonValue

from mux.contracts.errors import ProviderErrorCategory
from mux.drivers.gemini.mcp_auth import (
    RedactedCredential,
    contains_secret,
    model_request,
    refresh_bound_request,
)
from mux.errors import ProviderError

API_REVISION = "2026-05-20"
log = logging.getLogger(__name__)
type Object = dict[str, JsonValue]


@runtime_checkable
class TokenCountingTransport(Protocol):
    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int: ...


class Transport(Protocol):
    async def create(self, request: Mapping[str, JsonValue]) -> Object: ...
    async def get(self, interaction_id: str) -> Object: ...
    async def cancel(self, interaction_id: str) -> Object: ...
    async def download_snapshot(self, environment_id: str) -> bytes: ...
    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]: ...


def object_value(value: JsonValue) -> Object:
    if not isinstance(value, dict):
        raise ValueError("expected a provider object")
    return value


def string(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a provider identity")
    return value


def network_transform_present(value: JsonValue, *, in_network: bool = False) -> bool:
    """Presence-only inspection; never serialize a potentially echoed secret."""
    if isinstance(value, dict):
        return (in_network and "transform" in value) or any(
            network_transform_present(child, in_network=in_network or key == "network")
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(network_transform_present(child, in_network=in_network) for child in value)
    return False


def storage_request_token(request: Mapping[str, JsonValue]) -> RedactedCredential | None:
    """Remember only the dispatched mount credential, within the transport lifetime."""
    environment = request.get("environment")
    network = environment.get("network") if isinstance(environment, dict) else None
    rules = network.get("allowlist") if isinstance(network, dict) else None
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("domain") != "storage.googleapis.com":
            continue
        transform = rule.get("transform")
        header = transform.get("Authorization") if isinstance(transform, dict) else None
        if isinstance(header, str) and header.startswith("Bearer ") and header[7:]:
            return RedactedCredential(header[7:])
    return None


def _refuse_transform(path: str, reason: str) -> NoReturn:
    """Fail closed, logging only where and why (never the value)."""
    log.warning("gemini mount transform echo refused: path=%s reason=%s", path, reason)
    raise ProviderError("permission", retryable=False, native_code="gcs_transform_echo")


def echoed_mount_header(transform: object) -> str | None:
    """The Authorization value of an echoed mount transform, if it has our shape.

    We send `{"Authorization": "Bearer <t>"}`; Gemini echoes it as a list of
    header objects, `[{"Authorization": "Bearer <t>"}]` (F2-G r13). Either form
    with exactly that one header qualifies; anything else does not.
    """
    if isinstance(transform, list):
        if len(cast(list[object], transform)) != 1:
            return None
        transform = cast(list[object], transform)[0]
    if not isinstance(transform, dict):
        return None
    headers = cast(dict[str, object], transform)
    value = headers.get("Authorization")
    return value if set(headers) == {"Authorization"} and isinstance(value, str) else None


def scrub_mount_echo(value: Object, tokens: set[RedactedCredential]) -> Object:
    """Remove the echo of our own storage-mount transform, and nothing else.

    A completed interaction echoes the request's environment, including the
    mount rule's Authorization transform. Only that exact location is cleaned:
    `environment.network.allowlist[i].transform` on the interaction itself or
    on a stream event's `interaction`, for the storage domain, carrying a
    header this transport dispatched. Any other transform refuses.
    """
    result = copy.deepcopy(value)
    ours = {"Bearer " + token for token in tokens}
    nested = result.get("interaction")
    owners = [("$", result)]
    if isinstance(nested, dict):
        owners.append(("$.interaction", nested))
    for prefix, owner in owners:
        environment = owner.get("environment")
        network = environment.get("network") if isinstance(environment, dict) else None
        rules = network.get("allowlist") if isinstance(network, dict) else None
        for index, rule in enumerate(rules if isinstance(rules, list) else []):
            if not isinstance(rule, dict) or "transform" not in rule:
                continue
            path = f"{prefix}.environment.network.allowlist[{index}].transform"
            if rule.get("domain") != "storage.googleapis.com":
                _refuse_transform(path, "foreign_domain")
            header = echoed_mount_header(rule["transform"])
            if header is None:
                _refuse_transform(path, "unrecognized_shape")
            if header not in ours:
                _refuse_transform(path, "not_dispatched_by_this_transport")
            del rule["transform"]
    return result


def transform_paths(value: JsonValue, path: str = "$", *, in_network: bool = False) -> list[str]:
    """Key paths of network transforms (presence only, for diagnostics)."""
    found: list[str] = []
    if isinstance(value, dict):
        if in_network and "transform" in value:
            found.append(path + ".transform")
        for key, child in value.items():
            found += transform_paths(
                child, f"{path}.{key}", in_network=in_network or key == "network"
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found += transform_paths(child, f"{path}[{index}]", in_network=in_network)
    return found


def checked_storage_response(
    value: Object, *, protected: bool, tokens: set[RedactedCredential] | None = None
) -> Object:
    """Scrub our own echoed mount transform, then fail closed on any other trace."""
    if not protected:
        return value
    known = tokens or set()
    value = scrub_mount_echo(value, known)
    if network_transform_present(value):
        _refuse_transform(",".join(transform_paths(value)), "outside_mount_location")
    # The token anywhere else (model output, a tool result, another field) is a
    # leak: check decoded values and the re-encoded bytes of the scrubbed payload.
    encoded = json.dumps(value).encode()
    if any(contains_secret(value, t) or t.encode() in encoded for t in known):
        log.warning("gemini mount token echo refused: reason=token_outside_mount_location")
        raise ProviderError("permission", retryable=False, native_code="gcs_token_echo")
    return value


def high_demand(error: Exception) -> bool:
    """Gemini reports capacity blips as HTTP 500 api_error ("high demand").

    Other 5xx bodies, including a plain api_error, stay retryable upstream.
    """
    messages: list[object] = [getattr(error, "message", None)]
    # Interactions errors carry `body`; the google-genai APIError carries `details`.
    body: object = getattr(error, "body", None) or getattr(error, "details", None)
    if isinstance(body, dict):
        payload = cast(dict[str, object], body)
        nested = payload.get("error", payload)
        if isinstance(nested, dict):
            messages.append(cast(dict[str, object], nested).get("message"))
    return any(isinstance(m, str) and "high demand" in m.lower() for m in messages)


def normalize_error(error: Exception) -> ProviderError:
    status = (
        error.status_code
        if isinstance(error, APIStatusError)
        else (error.code if isinstance(error, genai_errors.APIError) else None)
    )
    categories: dict[int, ProviderErrorCategory] = {
        400: "invalid_request",
        401: "auth",
        403: "permission",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
        503: "overloaded",
    }
    if isinstance(error, APIConnectionError | httpx.HTTPError):
        return ProviderError("transient_network", retryable=True)
    # The HTTP status alone is safe to log and lets an unmapped status be told apart.
    http = None if status is None else f"http_{status}"
    if status is not None and status >= 500 and status != 503 and high_demand(error):
        # Distinct from 503: the Flash fallback chain moves models only on a 503.
        return ProviderError(
            "overloaded", retryable=True, native_code="high_demand", native_error_code=http
        )
    category = categories.get(status or 0, "upstream")
    return ProviderError(
        category,
        retryable=status == 429 or (status is not None and status >= 500),
        native_code="503" if status == 503 else None,
        native_error_code=http,
    )


async def provider_call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except (APIError, genai_errors.APIError, httpx.HTTPError) as error:
        raise normalize_error(error) from None


class OwnedStream[T](AsyncIterator[T]):
    """Close the opened connection even if iteration never begins."""

    def __init__(self, source: AsyncIterator[T], close: Callable[[], Awaitable[None]]) -> None:
        self._source, self._close, self._closed = source, close, False

    def __aiter__(self) -> OwnedStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._source.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()
            await self._close()


async def close_iterator(source: AsyncIterator[object]) -> None:
    close = getattr(source, "aclose", None)
    if close is not None:
        await close()


class SDKTransport:
    """Caller supplies a no-retry SDK client; no keys or raw SDK values leave this boundary.

    The pinned SDK supports `extra_body` for newer Antigravity configuration.
    Interactions retries are disabled here; the host also configures the
    supplied base client's HttpRetryOptions(attempts=1).
    """

    def __init__(
        self,
        client: genai.Client,
        *,
        protect_storage_credentials: bool = False,
        storage_tokens: set[RedactedCredential] | None = None,
    ) -> None:
        self._protect_storage_credentials = protect_storage_credentials
        self._storage_tokens: set[RedactedCredential] = (
            storage_tokens if storage_tokens is not None else set()
        )
        # google-genai 2.7 forwards attempts as Stainless max_retries, so
        # attempts=1 still retries once. Set the Interactions edge to zero.
        nextgen = client.aio._nextgen_client  # pyright: ignore[reportPrivateUsage]
        self._interactions = nextgen.with_options(max_retries=0).interactions
        self._files = client.aio.files
        self._models = client.aio.models

    def _protected_interaction(self, response: httpx.Response) -> Object:
        """A protected Interaction, scrubbed before the SDK parses or renders it.

        Plain JSON decoding renders nothing; SDK models (whose serializer
        warnings can quote values) only ever see the scrubbed payload.
        """
        from google.genai._interactions.types import Interaction

        try:
            raw = object_value(cast(JsonValue, json.loads(response.content)))
        except ValueError:
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_interaction"
            ) from None
        clean = checked_storage_response(raw, protected=True, tokens=self._storage_tokens)
        # The same loose construction `response.parse()` performs, on clean data.
        parsed = cast(Any, Interaction).construct(**clean)
        return object_value(
            cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True, warnings=False))
        )

    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int:
        from google.genai.types import CountTokensConfig, HttpOptions

        bounded = dict(request)
        if not model_request(bounded) or "previous_interaction_id" in bounded:
            raise ProviderError("invalid_request", retryable=False, native_code="unbounded_prompt")
        inputs = bounded.get("input")
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], dict)
            or set(inputs[0]) != {"type", "text"}
            or inputs[0].get("type") != "text"
            or not isinstance(inputs[0].get("text"), str)
        ):
            raise ProviderError("invalid_request", retryable=False, native_code="unbounded_prompt")
        system = bounded.get("system_instruction")
        if system is not None and not isinstance(system, str):
            raise ProviderError("invalid_request", retryable=False, native_code="unbounded_prompt")
        # The Developer API supports full request counting through
        # generateContentRequest; the SDK's system_instruction convenience
        # argument is Vertex-only. Use its supported extra_body escape hatch.
        prompt: Object = {
            "model": "models/" + string(bounded["model"]),
            "contents": [{"role": "user", "parts": [{"text": inputs[0]["text"]}]}],
        }
        if system is not None:
            prompt["systemInstruction"] = {"parts": [{"text": system}]}
        result = await provider_call(
            self._models.count_tokens(
                model=string(bounded["model"]),
                # The wire schema permits omitting contents for a full request.
                contents=None,  # pyright: ignore[reportArgumentType]
                config=CountTokensConfig(
                    http_options=HttpOptions(
                        extra_body={"generateContentRequest": prompt},
                    )
                ),
            )
        )
        if type(result.total_tokens) is not int or result.total_tokens <= 0:
            raise ProviderError("upstream", retryable=False, native_code="unknown_input_tokens")
        # total_tokens includes cached content; never subtract a cache discount
        # from the token envelope. Pricing can reserve every input at the full rate.
        return result.total_tokens

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await provider_call(self._files.download(file=f"files/environment-{environment_id}"))

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        # Omission enables provider defaults, including billable web tools.
        # Keep older saved requests explicit as well as freshly compiled agents.
        bounded = dict(request)
        bounded.setdefault("tools", [])
        if not isinstance(bounded["tools"], list):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="explicit_tools_required"
            )
        bounded = await refresh_bound_request(bounded)
        token = storage_request_token(bounded)
        if token is not None:
            self._storage_tokens.add(token)
        protected = (
            self._protect_storage_credentials
            or bool(self._storage_tokens)
            or network_transform_present(bounded)
        )
        if model_request(bounded):
            response = await provider_call(
                self._interactions.with_raw_response.create(
                    model=string(bounded["model"]),
                    input=[],
                    background=True,
                    store=True,
                    extra_body=bounded,
                    extra_headers={"Api-Revision": API_REVISION},
                )
            )
        else:
            response = await provider_call(
                self._interactions.with_raw_response.create(
                    agent=string(bounded["agent"]),
                    input=[],
                    background=True,
                    store=True,
                    extra_body=bounded,
                    extra_headers={"Api-Revision": API_REVISION},
                )
            )
        if protected:
            return self._protected_interaction(response.http_response)
        parsed = await response.parse()
        return object_value(cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True)))

    async def get(self, interaction_id: str) -> Object:
        response = await provider_call(
            self._interactions.with_raw_response.get(
                id=interaction_id,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        if self._protect_storage_credentials or self._storage_tokens:
            return self._protected_interaction(response.http_response)
        parsed = await response.parse()
        return object_value(cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True)))

    async def cancel(self, interaction_id: str) -> Object:
        response = await provider_call(
            self._interactions.with_raw_response.cancel(
                id=interaction_id,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        if self._protect_storage_credentials or self._storage_tokens:
            return self._protected_interaction(response.http_response)
        parsed = await response.parse()
        return object_value(cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True)))

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        from google.genai._interactions import omit

        source = await provider_call(
            self._interactions.get(
                id=interaction_id,
                stream=True,
                last_event_id=after if after is not None else omit,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )

        async def records() -> AsyncIterator[Object]:
            try:
                async for event in source:
                    if self._protect_storage_credentials or self._storage_tokens:
                        value = object_value(
                            cast(
                                JsonValue,
                                event.model_dump(mode="json", exclude_none=True, warnings=False),
                            )
                        )
                        yield checked_storage_response(
                            value, protected=True, tokens=self._storage_tokens
                        )
                    else:
                        yield object_value(
                            cast(JsonValue, event.model_dump(mode="json", exclude_none=True))
                        )
            except (APIError, httpx.HTTPError) as error:
                raise normalize_error(error) from None
            finally:
                await source.close()

        return OwnedStream(records(), source.close)
