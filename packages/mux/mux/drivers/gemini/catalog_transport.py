"""Private native managed-agent SDK boundary, selected only by the operator."""

from collections.abc import Mapping
from typing import Protocol, cast

from google import genai
from pydantic import JsonValue

from mux.drivers.gemini.transport import API_REVISION, Object, object_value, provider_call, string


class AgentCatalogTransport(Protocol):
    async def create_agent(self, definition: Mapping[str, JsonValue]) -> Object: ...
    async def get_agent(self, id_: str) -> Object: ...


class SDKAgentCatalogTransport:
    """An explicitly supplied, owned client; no credential or environment discovery.

    Native agents have no provider version/If-Match. The catalog creates new
    scoped IDs and checks GET independently rather than synthesizing a revision.
    """

    def __init__(self, client: genai.Client) -> None:
        nextgen = client.aio._nextgen_client  # pyright: ignore[reportPrivateUsage]
        self._agents = nextgen.with_options(max_retries=0).agents

    async def create_agent(self, definition: Mapping[str, JsonValue]) -> Object:
        response = await provider_call(
            self._agents.create(
                id=string(definition["id"]),
                base_agent=string(definition["base_agent"]),
                extra_body=dict(definition),
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        return object_value(cast(JsonValue, response.model_dump(mode="json", exclude_none=True)))

    async def get_agent(self, id_: str) -> Object:
        response = await provider_call(
            self._agents.get(id=id_, extra_headers={"Api-Revision": API_REVISION})
        )
        # Keep explicit nulls as evidence; absent SDK defaults are not readback.
        return object_value(cast(JsonValue, response.model_dump(mode="json", exclude_unset=True)))
