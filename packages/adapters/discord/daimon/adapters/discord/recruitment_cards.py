"""Human recruitment cards; persistent IDs are dispatched by the existing bot."""

from daimon.core.recruitment import Outbox, ThreadState
from daimon.core.recruitment_api_models import MatchResponse

import discord


def review_card(
    state: ThreadState, outgoing: Outbox | None
) -> tuple[discord.Embed, discord.ui.View]:
    embed = discord.Embed(
        title="Recruitment review",
        description=(state.summary or "Waiting for a complete candidate history scan.")[:2800],
    )
    embed.add_field(
        name="Status",
        value=f"{state.phase} · revision {state.revision[:8] or 'pending'}",
        inline=False,
    )
    if state.flags:
        embed.add_field(name="Review flags", value="\n".join(state.flags)[:800], inline=False)
    if state.changes:
        embed.add_field(
            name="Proposed updates",
            value="\n".join(
                f"{c['field']}: {c.get('value')}\n{c['source_message_url']}" for c in state.changes
            )[:800],
            inline=False,
        )
    if state.error:
        embed.add_field(name="Needs attention", value=state.error, inline=False)
    if outgoing:
        embed.add_field(
            name="Delivery",
            value=f"{outgoing.state}; attempts {outgoing.attempts}; "
            f"next {outgoing.next_attempt_at or 'manual retry'}",
            inline=False,
        )
    if state.notion_url:
        embed.add_field(name="Recruitment record", value=state.notion_url[:1000], inline=False)
    embed.set_footer(text=f"recruitment:{state.card_intent}")
    view = discord.ui.View(timeout=None)

    def button(label: str, action: str, *, disabled: bool = False) -> None:
        view.add_item(
            discord.ui.Button(
                label=label, custom_id=f"recruit:{state.revision}:{action}", disabled=disabled
            )
        )

    if outgoing and outgoing.state not in {"completed", "discarded", "expired"}:
        button("Retry", "retry")
        button(
            "Discard",
            "discard",
            disabled=bool(
                outgoing.attempts
                and outgoing.error
                not in {"validation_error", "payload_too_large", "candidate_match_conflict"}
            ),
        )
    elif state.phase == "review":
        ready = state.scan_complete and state.candidate is not None and not state.error
        if state.candidate_id:
            button("Confirm update", "update", disabled=not ready or not state.changes)
        else:
            match = MatchResponse.model_validate(state.matches) if state.matches else None
            button(
                "Create",
                "create",
                disabled=not ready or bool(match and match.status in {"exact", "linked"}),
            )
            if match:
                for candidate in match.candidates:
                    button(
                        f"Link {candidate.name}"[:80],
                        str(candidate.candidate_id),
                        disabled=not ready,
                    )
        button("Correct", "correct")
        button("Skip", "skip")
    else:
        button("Refresh review", "correct")
    return embed, view
