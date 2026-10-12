"""Discord rendering over the shared, authenticated fleet operations.

The host supplies its current-root loader. A card handle never supplies root
authority, and an operation acknowledgement never certifies native completion.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.adapters.discord.views import finish_control
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.fleet import TERMINAL, FleetRun, FleetWorker
from daimon.core.fleet_orchestration import Control, FleetOrchestrator, FleetRefused
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.neutral_resources import WorkerPermissions, WorkerSpecRefused
from daimon.core.posted_controls import CardButton, CardState, PostedCard

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot

type ParentLoader = Callable[
    [discord.Interaction | None], Awaitable[tuple[WorkerPermissions, TenantAccessPolicy]]
]
type Send = Callable[..., Awaitable[discord.Message]]
type Edit = Callable[..., Awaitable[discord.Message | None]]


def _literal(value: str, limit: int) -> str:
    return (
        discord.utils.escape_markdown(value[:limit])
        .replace("@", "@\u200b")
        .replace("<#", "<\u200b#")
    )


def build_fleet_view(
    run: FleetRun,
    workers: Sequence[FleetWorker],
    *,
    collected: bool = False,
) -> discord.ui.LayoutView:
    """Render only canonical scoped rows; no specs, excerpts or native handles."""
    if (
        run.platform != "discord"
        or len({worker.id for worker in workers}) != len(workers)
        or len(workers) > run.max_workers
        or any(worker.run_id != run.id or worker.tenant_id != run.tenant_id for worker in workers)
    ):
        raise FleetRefused("foreign_worker")
    state: CardState = (
        "applied"
        if collected
        else "refused"
        if run.status in ("cancelled", "failed")
        else "received"
    )
    buttons: list[CardButton] = []
    facts: list[str] = []
    for worker in sorted(workers, key=lambda row: row.ordinal):
        facts.append(
            f"{worker.ordinal}. **{_literal(worker.role, 60)}** · "
            f"{_literal(worker.backend, 20)} / {_literal(worker.model, 40)} — "
            f"{worker.status.replace('_', ' ')}"
        )
        if collected or run.status not in ("running", "collecting") or worker.status in TERMINAL:
            continue
        actions: tuple[tuple[Control, str], ...] = (("kill", "Stop"), ("resume", "Resume"))
        for action, label in actions:
            custom_id = f"fleet:{run.id.hex}:{worker.id.hex}:{worker.version}:{action}"
            if len(custom_id) > 100:
                raise FleetRefused("invalid_control")

            buttons.append(
                CardButton(label=f"{label} {worker.ordinal}", style="primary", custom_id=custom_id)
            )
    card = PostedCard(
        kind="fleet",
        state=state,
        headline="Fleet — collected" if collected else f"Fleet — {run.status}",
        facts=tuple(facts),
        buttons=tuple(buttons),
        footer=(
            f"Budget cap ${run.total_budget_usd} · "
            f"Deadline <t:{int(run.deadline_at.timestamp())}:f>"
        ),
    )
    return build_card_view(card, button_factory=FleetControl.from_button)


class FleetControl(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=r"fleet:(?P<run>[0-9a-f]{32}):(?P<worker>[0-9a-f]{32}):(?P<version>[0-9]+):(?P<action>kill|resume)",
):
    """The same durable, host-reconstructed control for live and restarted clients."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)
        match = self.template.fullmatch(button.custom_id or "")
        if match is None:
            raise FleetRefused("invalid_control")
        self.run_id = uuid.UUID(hex=match["run"])
        self.worker_id = uuid.UUID(hex=match["worker"])
        self.expected_version = int(match["version"])
        self.action: Control = cast(Control, match["action"])

    @classmethod
    def from_button(cls, button: CardButton) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary, label=button.label, custom_id=button.custom_id
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=getattr(item, "label", None),
                custom_id=match[0],
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("DaimonBot", interaction.client)
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable) or interaction.message is None:
            await interaction.response.send_message(
                "This fleet control is unavailable.", ephemeral=True
            )
            return

        async def edit(
            message: discord.Message,
            *,
            view: discord.ui.LayoutView,
            allowed_mentions: discord.AllowedMentions,
        ) -> discord.Message:
            return await message.edit(view=view, allowed_mentions=allowed_mentions)

        card = bot.fleet_card_for(
            thread=channel,
            requester_id=interaction.user.id,
            send=lambda: channel.send,
            edit=edit,
            run_id=self.run_id,
        )
        if card is None or card.run_id != self.run_id:
            await interaction.response.send_message(
                "This fleet control is unavailable.", ephemeral=True
            )
            return
        await card.control(
            interaction,
            worker_id=self.worker_id,
            action=self.action,
            expected_version=self.expected_version,
        )


class DiscordFleetCard:
    """One orchestrator card; all reads and controls use the same core owner."""

    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        service: FleetOrchestrator,
        load_parent: ParentLoader,
        send: Send,
        edit: Edit,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._run_id = run_id
        self._service = service
        self._load_parent = load_parent
        self._send = send
        self._edit = edit
        self._clock = clock
        self._message: discord.Message | None = None
        self._view: discord.ui.LayoutView | None = None
        self._lock = asyncio.Lock()

    @property
    def run_id(self) -> uuid.UUID:
        """A lookup identity; the shared owner still proves root/run authority."""
        return self._run_id

    async def _snapshot(
        self, interaction: discord.Interaction | None
    ) -> tuple[WorkerPermissions, TenantAccessPolicy, FleetRun, tuple[FleetWorker, ...]]:
        parent, policy = await self._load_parent(interaction)
        if interaction is not None and (
            parent.subject.platform_user_id != str(interaction.user.id)
            or interaction.guild_id is None
            or parent.fleet.scope.tenant_id
            != str(derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id)))
            or parent.fleet.root.channel.platform != "discord"
            or parent.fleet.root.thread_id != str(interaction.channel_id)
        ):
            raise FleetRefused("foreign_run")
        run, workers = await self._service.read_snapshot(parent, policy, self._run_id)
        return parent, policy, run, workers

    def _render(
        self, run: FleetRun, workers: tuple[FleetWorker, ...], *, collected: bool = False
    ) -> discord.ui.LayoutView:
        return build_fleet_view(
            run,
            workers,
            collected=collected,
        )

    async def refresh(self) -> None:
        """Called from the existing render loop, never from a native event tap."""
        async with self._lock:
            _, _, run, workers = await self._snapshot(None)
            view = self._render(run, workers)
            if self._message is None:
                self._message = await self._send(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await self._edit(
                    self._message, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            if self._view is not None:
                self._view.stop()
            self._view = view

    async def control(
        self,
        interaction: discord.Interaction,
        *,
        worker_id: uuid.UUID,
        action: Control,
        expected_version: int,
    ) -> bool:
        async with self._lock:
            try:
                parent, policy, _, _ = await self._snapshot(interaction)
                reply = await self._service.control(
                    parent,
                    policy,
                    run_id=self._run_id,
                    worker_id=worker_id,
                    action=action,
                    expected_version=expected_version,
                    operation_key=f"discord:fleet:{self._run_id}:{worker_id}:{expected_version}:{action}",
                    now=self._clock(),
                )
                if reply.get("accepted") is not True:
                    await interaction.response.send_message(
                        "This action is waiting for its dependencies.", ephemeral=True
                    )
                    return False
                run, workers = await self._service.read_snapshot(parent, policy, self._run_id)
                view = self._render(run, workers)
                await finish_control(
                    interaction, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
                self._view = view
                return True
            except (FleetRefused, WorkerSpecRefused):
                await interaction.response.send_message(
                    "This control is no longer available. Refresh the fleet card.",
                    ephemeral=True,
                )
                return False

    async def allow_summary(self) -> bool:
        """Gate the existing orchestrator answer on authoritative collect/review."""
        async with self._lock:
            try:
                parent, policy, run, workers = await self._snapshot(None)
                reply = await self._service.collect(parent, policy, run_id=self._run_id)
            except (FleetRefused, WorkerSpecRefused):
                return False
            ready = reply.get("status") == "ready"
            if ready and self._message is not None:
                view = self._render(run, workers, collected=True)
                await self._edit(
                    self._message, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
                if self._view is not None:
                    self._view.stop()
                self._view = view
            return ready
