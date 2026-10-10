"""One admin callback for the catalog runner: each frozen admin source to its binding.

The runner hands every `do: admin` operation here, in source order, with the
fixture values gathered so far. `tool: headless` with `call:
credential_submit.mcp_token` runs the real credential form
(`daimon.testing.headless_bindings`); every other source goes to the product
admin bindings (`daimon.testing.admin_bindings`). Anything neither can bind
comes back `pending` with a typed code, so the runner keeps its barrier.
A receipt records an executed operation, never a scenario verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx
from daimon.testing.admin_bindings import AdminBindings, AdminReceipt
from daimon.testing.headless_bindings import submit_mcp_token
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MCP_TOKEN_CALL = "credential_submit.mcp_token"
_ENV = re.compile(r"\{(env\.[a-z0-9_]+)\}")


@dataclass
class CatalogAdmin:
    """The runner's admin callback for one backend column of one disposable run."""

    backend: Provider
    sessions: async_sessionmaker[AsyncSession]
    # The served token-gated QA MCP host, for the credential form's probe.
    mcp_transport: httpx.AsyncBaseTransport | None = None
    product: AdminBindings | None = None

    async def __call__(self, source: Mapping[str, Any], values: Mapping[str, str]) -> AdminReceipt:
        if source.get("do") == "admin" and source.get("tool") == "headless":
            return await self._headless(source, values)
        if self.product is None:
            return self._pending("admin", "ADMIN_PRODUCT_BINDINGS_UNBOUND")
        return await self.product.execute(source, values)

    async def _headless(self, source: Mapping[str, Any], values: Mapping[str, str]) -> AdminReceipt:
        raw = source.get("args")
        if set(source) != {"do", "tool", "args"} or not isinstance(raw, dict):
            return self._pending("headless", "ADMIN_SOURCE_UNSUPPORTED")
        args = cast(dict[str, object], raw)
        if args.get("call") != MCP_TOKEN_CALL:
            return self._pending("headless", "ADMIN_HEADLESS_CALL_UNBOUND")
        if set(args) != {"call", "token", "url"} or not all(
            isinstance(args[k], str) for k in ("token", "url")
        ):
            return self._pending(MCP_TOKEN_CALL, "ADMIN_ARGUMENTS_UNBOUND")
        if self.mcp_transport is None:
            return self._pending(MCP_TOKEN_CALL, "ADMIN_MCP_HOST_UNBOUND")
        url = _ENV.sub(lambda m: values.get(m.group(1), m.group(0)), str(args["url"]))
        if "{" in url:
            return self._pending(MCP_TOKEN_CALL, "ADMIN_ARGUMENTS_UNBOUND")
        receipt = await submit_mcp_token(
            self.sessions, mcp_transport=self.mcp_transport, token=str(args["token"]), url=url
        )
        return AdminReceipt(
            status="executed",
            backend=self.backend,
            operation=MCP_TOKEN_CALL,
            observations={
                "outcome": receipt.outcome,
                "used": receipt.used,
                "probe_statuses": list(receipt.probe_statuses),
                "ma_requests": list(receipt.ma_requests),
            },
            values=receipt.values,
        )

    def _pending(self, operation: str, code: str) -> AdminReceipt:
        return AdminReceipt(status="pending", backend=self.backend, operation=operation, code=code)
