# Per-thread admission queue and turn tail

`ThreadQueue.tla` models one adapter process and one platform thread: the
in-memory `_processing` / `_pending` state, the ⌛-reaction enqueue, the drain
loop, the turn tail (clear the active-turn marker, dispatch queued task
continuations) and continuation dispatch requested from outside a turn by a
credential form submission. Every action boundary is an `await` in the asyncio
code; everything inside one action runs without yielding. Three mentions arrive
in order (`m1` and `m3` from author A, `m2` from B).

| Model item | Implementation |
| --- | --- |
| `Arrive`, `ReactDone` | Slack `_orchestrate` queue check ([`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py)); Discord `on_message` queue check ([`bot.py`](../../packages/adapters/discord/daimon/adapters/discord/bot.py)) |
| `DrainOrRelease`, `Partition` | Slack drain loop and `finally`; Discord `_drain_pending_mentions` and the mention `finally` |
| `Tail1`, `Tail2` | Slack `_run_thread_turn` tail: marker clear, then `_dispatch_continuations`; Discord continuation dispatch at turn end |
| `QueueHandoff` | a handoff tool call recording a `task_continuations` row mid-turn |
| `FormRecord`, `FormDispatch` | credential submission recording its continuation and calling `dispatch_continuations_in_thread` ([Slack](../../packages/adapters/slack/daimon/adapters/slack/credential_submissions.py), [Discord](../../packages/adapters/discord/daimon/adapters/discord/credential_modals.py)); the claim step: skip (and with #233 remember) a processing thread, else add it to `_processing` and run the continuation turn |
| `FormRelease` | the dispatch's `finally`: `_release_thread` on main; with `DrainAfterDispatch` (#237) the queued mentions are drained first, one turn per author |

## Run

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/thread_queue
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-thread-queue-tlc/$1" -config "$1.cfg" ThreadQueue.tla; }
for c in QueueBeforeReact HandoffClearFirst FormRedispatch FormDispatchDrain; do run $c; done
for c in DiscordReactFirst DrainMergedAuthors HandoffDispatchFirst FormDuringTail FormDispatchNoDrain; do run $c || test $? -eq 12; done
```

`ThreadGone.tla` (below) is a separate model; `formal/check.sh` runs every row in
`expected.tsv`, which pins its verdicts and state counts.

## Properties

| Invariant | Meaning |
| --- | --- |
| `PrincipalIsAuthor` | each message is answered in its own author's turn (session, vault, billing) |
| `NoStrandedMention` | once the thread is idle, every delivered mention has been answered |
| `HandoffOnDestination` | a handoff's first turn runs in the incoming agent's session |
| `NoStrandedContinuation` | a recorded continuation is not left pending once the thread is idle |

## Calibration

| Fix | Pre-fix config | Verdict | Fixed config | Verdict |
| --- | --- | --- | --- | --- |
| `997b3d8` partition the drain queue by author | `DrainMergedAuthors` | violates `PrincipalIsAuthor` | `QueueBeforeReact` | clean |
| `090ecc1` clear the marker before dispatching the handoff | `HandoffDispatchFirst` | violates `HandoffOnDestination` | `HandoffClearFirst` | clean |

## Regression rows (fixed on main)

These bugs were found while building the model and are fixed on main by
pymc-labs/daimon#228 and #233. The pre-fix configs stay as regression rows, not
independent calibrations: Slack's enqueue-before-reaction order (WR-05) predates
the public history, so `DiscordReactFirst` is confirmed by #228 rather than by a
recoverable past commit.

- **Discord stranded a mention queued while the turn ends** (`DiscordReactFirst`;
  fixed on main by #228). Before #228, Discord awaited the ⌛ reaction before appending to `_pending`; if the running
  turn drained and released the thread during that await, the message sat in
  `_pending` until the next mention. Found first by code reading; the model
  reproduces it. Fixed on main by pymc-labs/daimon#228.
- **A continuation recorded as the turn ends waited for the next turn**
  (`FormDuringTail`; fixed on main by #233). Before #233, a credential form submitted after the running turn's tail
  dispatch but before the thread is released calls
  `dispatch_continuations_in_thread`, which skips a processing thread; nothing
  dispatches the row until another turn in that thread completes. Trace: turn
  ends → marker cleared → continuations dispatched (none yet) → form records its
  continuation → dispatch skipped (thread processing) → thread released.
  `FormRedispatch` (a skipped dispatch re-runs when the thread is released) is
  clean. Fixed on main by pymc-labs/daimon#233. As in the code, the re-run is
  spawned at release and claims the thread again after an await
  (`FormDispatch`), so it can defer again if a mention claimed the thread first.
  `FormRedispatch` isolates the #233 fix and does not check
  `NoStrandedMention`; that property is checked by the #237 configurations
  below.

## Pre-#237 counterexample

- **A mention queued behind an out-of-turn dispatch was stranded**
  (`FormDispatchNoDrain`, violates `NoStrandedMention`; fixed in
  pymc-labs/daimon#237). Before that fix, `dispatch_continuations_in_thread` (Slack
  `app.py`, Discord `bot.py`) added the thread to `_processing` for the whole
  continuation turn, so a mention arriving then was appended to `_pending` and
  got ⌛. Its `finally` only called `_release_thread`, which discarded the slot
  and re-ran a deferred dispatch but never read `_pending`, so the mention
  waited for the next mention in that thread. Trace (7 states): form records its
  continuation → dispatch claims the thread → three mentions arrive and queue →
  dispatch releases the thread with all three still queued. The same holds for
  the #233 re-run, which goes through the same function.
  `FormDispatchDrain` (drain `_pending` before releasing, now on main) is
  clean with all four invariants. The config differs from `FormDispatchNoDrain`
  only in `DrainAfterDispatch`. #237's Discord path leaves the queue for the
  next turn when the dispatch raises; a raising dispatch is not modelled.

## Bounds and assumptions

- One process, one thread, three mentions from two authors, one handoff and one
  form submission. The continuation turn an out-of-turn dispatch runs is one
  step between its claim and its release; mentions can arrive in between. A drained batch's failure is not modelled; `997b3d8`'s
  per-author error isolation is outside these properties.
- Cross-process behaviour (restart, orphan sweep, MA session state) is in
  [`adapter_recovery/AdapterOverlap.tla`](../adapter_recovery/README.md).
- A passing TLC run checks only this finite abstraction.

## A turn whose thread is deleted (`ThreadGone.tla`, pymc-labs/daimon#809)

Question: once a turn's thread is gone (Discord removed it, seen either as a
`10003 Unknown Channel` on any write or as the gateway delete event), can

1. anything be posted to the parent channel for that turn or its queue,
2. a queued turn start,
3. an admitted message's admission row stay unfinished once the thread is idle
   (so #764 startup replay dispatches it again), or
4. a write be issued to the thread after the signal was seen?

`ThreadGone.tla` models the Discord channel-mention path: one mention (`m1`,
author A) opens a thread, and two follow-ups (`m2` from B, `m3` from A) queue
behind it, so the drain composes two turns. It is a separate module because
`ThreadQueue.tla` has no thread lifecycle, turn writes or admission rows;
`ThreadQueue.tla` and its configs are untouched and keep their verdicts and
state counts. Constants select current main or the #809 design: all knobs off
is main, all on is the design, and each ablation turns one knob back.

| Model action | Code (main at `8d93a0b96`) |
| --- | --- |
| `Init` | the origin turn after `_processing.add(opened.id)` and the claimed row: `bot.py:3685`, `discord_message_admissions.py:14-72`, `bot.py:2332` |
| `Delete` | Discord removes the thread; nothing in Daimon (#809). `GatewayDelete`: `on_raw_thread_delete`, `bot.py:3408` (records the lifecycle row only; the turn is not told) |
| `Claim`, `Enqueue` | thread-branch `on_message`: `claim_message` (`bot.py:2332-2343`), queue check and append (`bot.py:2381-2383`, `2516-2531`); the ⌛ reaction add is not modelled |
| `Intent` | `post_initial_turn_card` commits the card intent and `finish_messages` before any write (`turn_card_recovery.py:91`, called at `bot.py:3781`) |
| `PostCard` | `lifecycle.post_initial()` (`turn_card_recovery.py:115`); a failure raises out of `_orchestrate` into `bot.py:2700-2713` |
| `Progress` | the render loop's edit; failures are logged and swallowed (`driver.py:812-823`, `lifecycle.py:880-883`) |
| `Terminal` | `on_terminal_success` / `on_terminal_failure` (`driver.py:1651-1660`); on a gone thread it raises (#809 log: `turn.completed` then `turn.failed 400 Unknown Channel`) |
| `OtherFailure` | any non-thread failure caught at `bot.py:2700-2713` |
| `ErrCard`, `ErrSend` | `_render_turn_error`: `surface.end_card`, then `safe_thread_send` (`bot.py:2745-2761`, `thread_send.py:13-34`, which re-raises anything but 50083) |
| `TurnEnd` | `_handle_mention` finally: `_finish_admitted_messages` (`bot.py:2714-2719`); a drained turn's `run` finally clears reactions (`bot.py:2623-2626`) |
| `Drain`, `DrainEmpty` | `ThreadQueue.drain` (`thread_queue.py:49-53`) via `_drain_pending_mentions` (`bot.py:2590-2633`), one turn per author |
| `StopDrain`, `StopFinish` | design only (step 4 of the #809 design): stop before starting a turn, finish the unrun rows, clear reactions |
| `Unwind` | an escaping exception: the drain `finally` (`bot.py:2630-2633`) |
| `Exit` | channel-branch `finally`: `_release_thread`, `_pending.pop`, reaction clear (`bot.py:2471-2478`) |
| `Prologue` | `on_message` except clauses: `_handle_prologue_failure` posts into `message.channel`, the parent channel (`bot.py:2490-2493`, `2559-2584`) |
| `Finish1` | `on_message` outer finally finishes the origin row (`bot.py:2494-2496`) |

Knobs: `GuardWrites` (no card, progress or error write once the signal is
seen), `GoneIsNotFailure` (a 10003 from a turn write is not a turn failure and
is never raised), `DrainStop` (`none` | `batch` | `group`: where the drain
checks the signal), `FinishUnrun` (the stopped drain finishes the rows it will
not run), `FinishAtRelease` (the final `_pending.pop` finishes its rows),
`ClearReactionsOnGone` (reaction removal is still attempted after the signal),
`WithOtherFailure` (a turn may also fail for a reason unrelated to the thread).

### Properties (ThreadGone)

| Invariant | Meaning |
| --- | --- |
| `NoParentPostAfterThread` | once the thread exists, nothing for the turn or its queue is posted in the parent channel (including the error fallback) |
| `NoParentPostAfterCard` | the same, for a turn whose status card was already up: the #809 path |
| `NoTurnAfterGone` | no queued turn begins after the adapter has seen the thread is gone |
| `NoWriteAfterGoneObserved` | no write is issued to the thread after the adapter has seen it is gone (a write already in flight when the signal arrives is not counted) |
| `AdmissionsFinishedWhenIdle` | once the handler has returned and no `on_message` is in flight, every claimed message's row is finished |

### Results (ThreadGone)

Checked within these bounds, one TLC worker; state counts are pinned in
`expected.tsv`.

| Config | Verdict | States | What it shows |
| --- | --- | ---: | --- |
| `GoneMain` | violates `NoParentPostAfterThread` | 271 | current main: the error render raises and the parent channel gets an error |
| `GoneMainIncident` | violates `NoParentPostAfterCard` | 651 | current main, the #809 path (card up, terminal write raises) |
| `GoneMainStart` | violates `NoTurnAfterGone` | 489 | current main: the next queued turn starts after the delete event |
| `GoneMainWrite` | violates `NoWriteAfterGoneObserved` | 35 | current main: the turn keeps writing after the delete event |
| `GoneMainRows` | violates `AdmissionsFinishedWhenIdle` | 561 | current main: the escape skips the drain; the queued row stays pending |
| `GoneDesign` | clean | 1206 | the #809 design, all five invariants |
| `GoneDesignNoFinish` | violates `AdmissionsFinishedWhenIdle` | 649 | design without finishing the unrun rows |
| `GoneDesignBatchStop` | violates `NoTurnAfterGone` | 990 | design checking the signal only when the queue is popped |
| `GoneDesignNoGuard` | violates `NoWriteAfterGoneObserved` | 52 | design without the write guard |
| `GoneDesignNoSwallow` | violates `NoParentPostAfterThread` | 304 | design where a 10003 on the error send still raises |
| `GoneDesignReactions` | violates `NoWriteAfterGoneObserved` | 408 | the design as first written (best-effort reaction removal after the signal) |
| `GoneDesignNoReleaseFinish` | violates `AdmissionsFinishedWhenIdle` | 891 | design without finishing the final queue pop |
| `GoneWitnessMidGroup` | violates `WitnessNoMidGroupStop` | 990 | witness: the signal can arrive between turns of one popped batch and the stop cuts the rest |
| `GoneWitnessFinishOnStop` | violates `WitnessNoFinishOnStop` | 408 | witness: the stop path finishes queued rows |

The `GoneMain*` configs set `WithOtherFailure = FALSE` so that the shortest
trace is the thread-gone path itself; the design and ablation configs set it to
`TRUE`, which only adds behaviours. The five checked invariants each have a
failing config next to the clean one, and each design element has an ablation
that fails, so `GoneDesign` cannot be clean by being blind to the bug.

### Counterexamples in plain words

- **Parent-channel error (`GoneMain`, `GoneMainIncident`).** (1) A mention in a
  channel opens a thread and the turn posts its status card. (2) Discord deletes
  the thread. (3) The turn finishes and its final write to the thread fails
  with 10003; that exception is not swallowed. (4) The error handler tries to
  edit the card, then to send the error into the thread; both fail, and the
  second failure escapes `_handle_mention`. (5) The drain for the thread is
  skipped, the thread is released, and `on_message` treats the escape as a
  prologue failure and posts the error into the parent channel. `GoneMain`
  reaches the same escape one step earlier, with the thread deleted before the
  card is posted.
- **A queued turn starts in a gone thread (`GoneMainStart`).** (1) Follow-up
  `m2` queues behind the origin turn. (2) The origin turn finishes normally,
  then Discord deletes the thread and the gateway delete event arrives. (3) The
  drain does not look at the event, starts `m2`'s turn, commits its card
  intent and tries to post the card. On main this fails and escapes (the first
  story); no model turn runs because the card post gates it, but the start
  step, the intent commit and the admission are done.
- **Queued rows left pending (`GoneMainRows`).** (1) `m2` is claimed (its row is
  pending) and queued. (2) The thread is deleted; the origin turn's card post
  or final write fails and the error escapes. (3) The drain never runs, so
  `m2` is popped by the channel `finally` (reaction cleared, row untouched).
  (4) The handler returns with `m2`'s row pending; #764 startup replay would
  dispatch it again into a thread that does not exist.
- **Writes after the signal (`GoneMainWrite`).** The delete event is seen and
  the origin turn still posts its card, and later edits, sends and reaction
  removals, to the thread.
- **Design variants.**
  `GoneDesignBatchStop`: `m2` and `m3` are popped as two turns; the thread is
  deleted during `m2`'s turn; checking only when the queue is popped lets `m3`
  start. `GoneDesignNoFinish`: the stopped drain drops `m2` and `m3` without
  finishing their rows. `GoneDesignNoReleaseFinish`: `m3` was claimed before
  the delete but is enqueued after the stop popped the queue and before the
  thread is released, so the final pop collects it and nothing finishes its
  row. `GoneDesignNoSwallow`: the thread is deleted while the turn is failing
  for an unrelated reason; the error send is the first write to see 10003 and
  it raises, so the parent channel gets an error again. `GoneDesignReactions`:
  once the signal is seen the stop path still removes the queued messages'
  reactions, which are writes to the gone thread.

### Assumptions and bounds (ThreadGone)

| Item | Source and status |
| --- | --- |
| Design behaviour | the #809 design (stop writes on the signal, never post elsewhere, stop the drain mid-group, finish queued admission rows); the model checks the design, not an implementation |
| Discord answers 10003 to every later write (post, edit, reaction) once the thread is deleted | unsupported; from the #809 log (edits and a send both failed) and to be confirmed on staging with a real delete |
| The final terminal write raises into `_orchestrate` on a gone thread | supported by the #809 log (`turn.completed`, then `turn.failed 400 Unknown Channel`), not traced to a single line; `lifecycle.py` terminal path swallows some edit errors (`turn.card_write_dropped`) |
| Progress edit failures are swallowed | `driver.py:812-823` |
| Every thread write that can happen is a card post, progress edit, terminal write, error card edit, error send or reaction removal | read from `bot.py` and `lifecycle.py`; webhook-transport fallback sends and `confirm_write` prompts are not modelled |
| The parent-channel post is allowed (`post_state.may_post`) | assumed; a protected channel hears nothing (`bot.py:2310`) |
| Parent-channel removal of the origin message's 👀 reaction is not a post and not a thread write | `bot.py:2677, 2725`; not modelled |
| Scaling | one progress edit stands for the ten in #809; two follow-ups with distinct authors give two drain turns; one delete; no retries of the abstract steps |
| Signal seen means: a 10003 on a write, or `on_raw_thread_delete` | design signals; 10015 (webhook gone) and 50083 (archived) are not modelled, so "Closed" is not covered |

Not modelled: the thread-branch handler (same drain and `finally`, a different
`on_message` branch), thread creation failure (`_ThreadOpenFailed`),
`mark_dead` and the active-turn marker (step 5 of the design), billing of an
interrupted turn, the interrupt itself (the model ends the turn at its next
step, since the model-side cost is invisible here), the ⌛ reaction add after
`Enqueue`, a follow-up that arrives after the thread is released (it starts its
own turn; its row is finished by that turn), orphan recovery, and
cross-process behaviour. `GoneDesign` assumes `GoneIsNotFailure` covers every
caller of a thread write, not only `_render_turn_error`.

Replay status: provisional. No counterexample has been replayed against the
real adapter yet, no staging contract test of the 10003 assumption has been
run, and no production trace has been checked against the model.
