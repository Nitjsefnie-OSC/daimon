"""Transactional Gemini payload persistence on the existing application state DB."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from daimon.core._models import Account, GeminiRuntimeState
from mux.contracts.ids import Scope
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class Payload:
    body: str | None
    save: Callable[[str], Awaitable[None]]


class PayloadDatabase(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Payload]: ...


class PostgresPayloadDatabase:
    """One locked payload row; records and holds use independent transactions."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        scope: Scope,
        project: str,
        kind: Literal["records", "holds"],
    ) -> None:
        if scope.is_platform or scope.is_legacy_host_authorized or not project.strip():
            raise ValueError("PostgreSQL Gemini state requires a tenant account and project")
        self._sessions = sessions
        self._key = {
            "tenant_id": UUID(scope.tenant_id),
            "account_id": UUID(scope.account_id),
            "project": project,
            "kind": kind,
        }

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Payload]:
        async with self._sessions.begin() as session:
            actor = await session.scalar(
                select(Account.id)
                .where(
                    Account.id == self._key["account_id"],
                    Account.tenant_id == self._key["tenant_id"],
                )
                .with_for_update(read=True, key_share=True)
            )
            if actor is None:
                raise ValueError("Gemini state account is unavailable")
            # ON CONFLICT serializes initial insertion too; the row lock below
            # covers the entire decode/edit/encode transaction across processes.
            await session.execute(
                insert(GeminiRuntimeState).values(**self._key).on_conflict_do_nothing()
            )
            predicates = (
                GeminiRuntimeState.tenant_id == self._key["tenant_id"],
                GeminiRuntimeState.account_id == self._key["account_id"],
                GeminiRuntimeState.project == self._key["project"],
                GeminiRuntimeState.kind == self._key["kind"],
            )
            row = (
                await session.execute(
                    select(GeminiRuntimeState.body).where(*predicates).with_for_update()
                )
            ).one()

            async def save(body: str) -> None:
                await session.execute(
                    update(GeminiRuntimeState).where(*predicates).values(body=body)
                )

            yield Payload(row.body, save)
