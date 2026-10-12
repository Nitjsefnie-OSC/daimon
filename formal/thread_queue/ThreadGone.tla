------------------------------ MODULE ThreadGone ------------------------------
(***************************************************************************)
(* One Discord channel mention opens a thread; two follow-ups (m2 from B,   *)
(* m3 from A) queue behind it. The thread can be deleted on Discord's side *)
(* at any time. The model asks, once the adapter has seen the thread is    *)
(* gone (a 10003 from any write, or the gateway delete event), whether     *)
(* anything is posted to the parent channel, a queued turn starts, an      *)
(* admitted message's admission row stays unfinished once the thread is    *)
(* idle, or a write is issued to the thread.                               *)
(*                                                                         *)
(* Kept apart from ThreadQueue.tla: that model has no thread lifecycle, no *)
(* turn writes and no admission rows. Every action boundary is an await in *)
(* the asyncio code; everything inside one action runs without yielding.   *)
(*                                                                         *)
(* Sources (line numbers are main at 8d93a0b96):                           *)
(*   packages/adapters/discord/daimon/adapters/discord/bot.py              *)
(*   packages/adapters/discord/daimon/adapters/discord/turn_card_recovery.py*)
(*   packages/core/daimon/core/turn/driver.py, turn/thread_queue.py        *)
(* The constants select current main (all knobs off) or the #809 design   *)
(* (all knobs on), one knob at a time for the ablation configs.            *)
(* README.md has the action-to-code table.                                 *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    GuardWrites,           \* design 1-2: once the gone signal is seen, no card, progress or error write is issued
    GoneIsNotFailure,      \* design 3: a 10003 from a turn write is not a turn failure; nothing is rendered or raised
    DrainStop,             \* design 4: "none" (main) | "batch" (check when the queue is popped) | "group" (before every turn)
    FinishUnrun,           \* design 4: finish the admission rows of the turns the stopped drain will not run
    FinishAtRelease,       \* finish the rows of whatever the final `_pending.pop` collects
    ClearReactionsOnGone,  \* TRUE: queued-message reaction removal is still attempted after the signal (main; the design as first written)
    WithOtherFailure       \* a turn may also fail for a reason unrelated to the thread (DB or upstream error)

Msgs == {"m1", "m2", "m3"}
Author == [m \in Msgs |-> IF m = "m2" THEN "B" ELSE "A"]   \* distinct authors: each queued message is its own drain group

VARIABLES
    thr,          \* Discord's truth: live | deleted
    known,        \* the adapter has seen the signal: a 10003 on a write, or on_raw_thread_delete (bot.py:3408)
    hp,           \* the on_message channel-branch handler: turn | drain | stopping | unwind | exit | prologue | final1 | done
    tp,           \* the running turn: none | intent | post | run | terminal | err1 | err2 | end
    cur,          \* the message the running turn answers (m1 origin; a queued message in a drain turn)
    pe,           \* a progress edit has been issued this turn (one stands for the ten in #809)
    cardUp,       \* the turn's status card exists (so surface.end_card is set, bot.py:3826)
    esc,          \* an exception escaped _handle_mention
    pending,      \* _pending[thread]: queued messages, in order
    groups,       \* the popped batch's turns still to run
    claiming,     \* on_message handlers past claim_message (bot.py:2332), before the queue check (bot.py:2381)
    row,          \* admission row of each message: none | pending | finished
    processing,   \* thread id in _processing
    parentPosts,  \* messages posted to the parent channel
    bad,          \* ghost: a write was issued to the thread after the signal was seen
    badStart,     \* ghost: a turn began after the signal was seen
    midStop,      \* witness: the drain stopped with turns of an already-popped batch unrun
    finStop,      \* witness: the stop path finished rows
    limbo         \* messages the stopped drain popped, waiting for their rows to be finished

vars == <<thr, known, hp, tp, cur, pe, cardUp, esc, pending, groups, claiming, row,
          processing, parentPosts, bad, badStart, midStop, finStop, limbo>>

Range(s) == {s[i] : i \in 1..Len(s)}
Fin(r, S) == [m \in Msgs |-> IF m \in S THEN "finished" ELSE r[m]]

WriteOK == ~(GuardWrites /\ known)
ReactOK == ClearReactionsOnGone \/ ~known
\* A write to the thread is issued now. If the thread is gone Discord answers 10003, and the
\* adapter has now seen it; a write issued when it had already seen it is the ghost violation.
Touch == known' = (known \/ thr = "deleted") /\ bad' = (bad \/ known)
Untouched == UNCHANGED <<known, bad>>
ReactWrite(somethingToClear) == IF somethingToClear /\ ReactOK THEN Touch ELSE Untouched

Init ==
    \* _processing.add(opened.id), bot.py:3685, then the origin turn starts in the channel branch
    /\ thr = "live" /\ known = FALSE
    /\ hp = "turn" /\ tp = "intent" /\ cur = "m1" /\ pe = FALSE /\ cardUp = FALSE /\ esc = FALSE
    /\ pending = <<>> /\ groups = <<>> /\ claiming = {}
    /\ row = [m \in Msgs |-> IF m = "m1" THEN "pending" ELSE "none"]   \* claim_message, bot.py:2332
    /\ processing = TRUE /\ parentPosts = 0
    /\ bad = FALSE /\ badStart = FALSE /\ midStop = FALSE /\ finStop = FALSE /\ limbo = {}

(* ---- the world ---- *)
\* Discord removes the thread (no Daimon involvement, #809).
Delete ==
    /\ thr = "live" /\ hp # "done"
    /\ thr' = "deleted"
    /\ UNCHANGED <<known, hp, tp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

\* on_raw_thread_delete, bot.py:3408. On main it only records the lifecycle row; the turn is not told.
GatewayDelete ==
    /\ thr = "deleted" /\ ~known /\ hp # "done"
    /\ known' = TRUE
    /\ UNCHANGED <<thr, hp, tp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

(* ---- a follow-up arrives in the thread (on_message, thread branch) ---- *)
\* Discord delivers it only while the thread exists. claim_message commits the row pending (bot.py:2332-2343).
Claim(m) ==
    /\ m \in {"m2", "m3"} /\ row[m] = "none" /\ thr = "live" /\ hp # "done"
    /\ row' = [row EXCEPT ![m] = "pending"] /\ claiming' = claiming \cup {m}
    /\ UNCHANGED <<thr, known, hp, tp, cur, pe, cardUp, esc, pending, groups, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

\* The queue check and the append run without an await (bot.py:2381, 2516-2531). Past release the
\* message starts its own turn (not modelled; its row is finished by that turn).
Enqueue(m) ==
    /\ m \in claiming
    /\ claiming' = claiming \ {m}
    /\ IF processing THEN /\ pending' = Append(pending, m) /\ UNCHANGED row
                     ELSE /\ row' = Fin(row, {m}) /\ UNCHANGED pending
    /\ UNCHANGED <<thr, known, hp, tp, cur, pe, cardUp, esc, groups, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

(* ---- the running turn: _handle_mention -> _orchestrate (bot.py:2635, 3453) ---- *)
InTurn == hp \in {"turn", "drain"}

\* post_initial_turn_card commits the card intent and finishes the turn's admission rows before
\* the first Discord write (turn_card_recovery.py:91, via bot.py:3781).
Intent ==
    /\ InTurn /\ tp = "intent"
    /\ row' = Fin(row, {cur}) /\ tp' = "post"
    /\ UNCHANGED <<thr, known, hp, cur, pe, cardUp, esc, pending, groups, claiming, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

\* lifecycle.post_initial() (turn_card_recovery.py:115). A failure raises out of _orchestrate.
PostCard ==
    /\ InTurn /\ tp = "post"
    /\ IF WriteOK
          THEN IF thr = "live"
                  THEN /\ tp' = "run" /\ cardUp' = TRUE /\ Touch
                  ELSE /\ cardUp' = FALSE /\ Touch
                       /\ tp' = IF GoneIsNotFailure THEN "end" ELSE "err2"   \* the except clauses, bot.py:2700-2713
          ELSE /\ tp' = "end" /\ UNCHANGED <<cardUp, known, bad>>
    /\ UNCHANGED <<thr, hp, cur, pe, esc, pending, groups, claiming, row, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

\* The render loop's progress edit. A failure is logged and swallowed (driver.py:812-823,
\* turn.progress_edit_failed, lifecycle.py:880-883); the turn keeps running.
Progress ==
    /\ InTurn /\ tp = "run" /\ ~pe /\ WriteOK
    /\ pe' = TRUE /\ Touch
    /\ UNCHANGED <<thr, hp, tp, cur, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

RunEnd ==
    /\ InTurn /\ tp = "run"
    /\ tp' = "terminal"
    /\ UNCHANGED <<thr, known, hp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

\* A failure unrelated to the thread (DB, upstream): bot.py:2700-2713 renders it.
OtherFailure ==
    /\ WithOtherFailure /\ InTurn /\ tp \in {"intent", "run"}
    /\ tp' = IF cardUp THEN "err1" ELSE "err2"
    /\ UNCHANGED <<thr, known, hp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

\* on_terminal_success / on_terminal_failure (driver.py:1651-1660). On a gone thread the terminal
\* write raises (the #809 log: turn.completed, then turn.failed 400 Unknown Channel, 14.6 s -> 14.8 s).
Terminal ==
    /\ InTurn /\ tp = "terminal"
    /\ IF WriteOK
          THEN IF thr = "live"
                  THEN /\ tp' = "end" /\ Touch
                  ELSE /\ Touch
                       /\ tp' = IF GoneIsNotFailure THEN "end" ELSE "err1"
          ELSE /\ tp' = "end" /\ Untouched
    /\ UNCHANGED <<thr, hp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

\* _render_turn_error, first half: surface.end_card edits the status card (bot.py:2745-2750). A
\* failure is caught and logged (turn.error_card_failed). With the signal seen, the design returns.
ErrCard ==
    /\ InTurn /\ tp = "err1"
    /\ IF WriteOK
          THEN IF thr = "live"
                  THEN /\ tp' = "end" /\ Touch
                  ELSE /\ tp' = "err2" /\ Touch
          ELSE /\ tp' = "end" /\ Untouched
    /\ UNCHANGED <<thr, hp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

\* _render_turn_error, second half: safe_thread_send into the thread (bot.py:2751-2761; thread_send.py
\* re-raises anything but 50083). On main the 404 escapes _handle_mention.
ErrSend ==
    /\ InTurn /\ tp = "err2"
    /\ IF WriteOK
          THEN IF thr = "live"
                  THEN /\ tp' = "end" /\ Touch /\ UNCHANGED esc
                  ELSE /\ Touch /\ tp' = "end"
                       /\ esc' = IF GoneIsNotFailure THEN esc ELSE TRUE
          ELSE /\ tp' = "end" /\ Untouched /\ UNCHANGED esc
    /\ UNCHANGED <<thr, hp, cur, pe, cardUp, pending, groups, claiming, row, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

\* _handle_mention's finally (bot.py:2714-2719): finish the turn's rows. A drained turn's `run`
\* then clears the queued messages' reactions (bot.py:2623-2626; thread writes). An escaped
\* exception continues to unwind; otherwise the drain loop goes on.
TurnEnd ==
    /\ InTurn /\ tp = "end"
    /\ row' = Fin(row, {cur})
    /\ ReactWrite(cur # "m1")
    /\ tp' = "none" /\ cur' = "none"
    /\ hp' = IF esc THEN "unwind" ELSE "drain"
    /\ UNCHANGED <<thr, pe, cardUp, esc, pending, groups, claiming, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

(* ---- the drain: _drain_pending_mentions -> ThreadQueue.drain (bot.py:2590, thread_queue.py:49-53) ---- *)
StartTurn(m, newGroups, newPending) ==
    /\ hp' = "drain" /\ tp' = "intent" /\ cur' = m /\ pe' = FALSE /\ cardUp' = FALSE
    /\ groups' = newGroups /\ pending' = newPending
    /\ badStart' = (badStart \/ known)

StopDrain ==
    /\ hp' = "stopping"
    /\ limbo' = Range(pending) \cup Range(groups)
    /\ pending' = <<>> /\ groups' = <<>>
    /\ midStop' = (midStop \/ groups # <<>>)

\* The exit with no await: the drain's empty check, the thread release and the final pop
\* (bot.py:2476-2478, 2631-2633) run together. An escaped exception then reaches on_message's
\* except clauses (bot.py:2490-2493) before the outer finally.
ExitBody ==
    /\ processing' = FALSE
    /\ pending' = <<>>
    /\ row' = IF FinishAtRelease THEN Fin(row, Range(pending)) ELSE row
    /\ ReactWrite(pending # <<>>)
    /\ hp' = IF esc THEN "prologue" ELSE "final1"

Drain ==
    /\ hp = "drain" /\ tp = "none"
    /\ \/ \* turns of the popped batch remain: run the next one (`for turn in turns`, thread_queue.py:51)
          /\ groups # <<>>
          /\ IF DrainStop = "group" /\ known
                THEN /\ StopDrain /\ UNCHANGED <<tp, cur, pe, cardUp, badStart>>
                ELSE /\ StartTurn(Head(groups), Tail(groups), pending) /\ UNCHANGED <<midStop, limbo>>
       \/ \* batch done: pop the queue and compose one turn per author (thread_queue.py:53)
          /\ groups = <<>> /\ pending # <<>>
          /\ IF DrainStop \in {"batch", "group"} /\ known
                THEN /\ StopDrain /\ UNCHANGED <<tp, cur, pe, cardUp, badStart>>
                ELSE /\ StartTurn(Head(pending), Tail(pending), <<>>) /\ UNCHANGED <<midStop, limbo>>
    /\ UNCHANGED <<thr, known, esc, claiming, row, processing, parentPosts, bad, finStop>>

\* Nothing queued: leave the drain and release the thread in one step.
DrainEmpty ==
    /\ hp = "drain" /\ tp = "none" /\ groups = <<>> /\ pending = <<>>
    /\ ExitBody
    /\ UNCHANGED <<thr, tp, cur, pe, cardUp, esc, groups, claiming, parentPosts, badStart,
                   midStop, finStop, limbo>>

\* Design: the stopped drain finishes the rows of the turns it will not run (a DB write, so an
\* await), and clears their reactions.
StopFinish ==
    /\ hp = "stopping"
    /\ row' = IF FinishUnrun THEN Fin(row, limbo) ELSE row
    /\ finStop' = (finStop \/ (FinishUnrun /\ limbo # {}))
    /\ ReactWrite(limbo # {})
    /\ limbo' = {} /\ hp' = "exit"
    /\ UNCHANGED <<thr, tp, cur, pe, cardUp, esc, pending, groups, claiming, processing,
                   parentPosts, badStart, midStop>>

\* An exception escaped a turn: the drain's finally clears the reactions of the batch's unrun
\* messages (bot.py:2630-2633; thread writes) and the batch is dropped, rows untouched.
Unwind ==
    /\ hp = "unwind"
    /\ ReactWrite(groups # <<>>)
    /\ groups' = <<>> /\ hp' = "exit"
    /\ UNCHANGED <<thr, tp, cur, pe, cardUp, esc, pending, claiming, row, processing,
                   parentPosts, badStart, midStop, finStop, limbo>>

Exit ==
    /\ hp = "exit"
    /\ ExitBody
    /\ UNCHANGED <<thr, tp, cur, pe, cardUp, esc, groups, claiming, parentPosts, badStart,
                   midStop, finStop, limbo>>

\* _handle_prologue_failure posts render_error into message.channel, the PARENT channel
\* (bot.py:2559-2584), assuming the channel may be posted in.
Prologue ==
    /\ hp = "prologue"
    /\ parentPosts' = parentPosts + 1 /\ hp' = "final1"
    /\ UNCHANGED <<thr, known, tp, cur, pe, cardUp, esc, pending, groups, claiming, row, processing,
                   bad, badStart, midStop, finStop, limbo>>

\* on_message's outer finally finishes the origin message's row (bot.py:2494-2496).
Finish1 ==
    /\ hp = "final1"
    /\ row' = Fin(row, {"m1"}) /\ hp' = "done"
    /\ UNCHANGED <<thr, known, tp, cur, pe, cardUp, esc, pending, groups, claiming, processing,
                   parentPosts, bad, badStart, midStop, finStop, limbo>>

Quiescent == hp = "done" /\ claiming = {}
Stutter == Quiescent /\ UNCHANGED vars

Next ==
    \/ Delete \/ GatewayDelete \/ (\E m \in {"m2", "m3"} : Claim(m) \/ Enqueue(m))
    \/ Intent \/ PostCard \/ Progress \/ RunEnd \/ OtherFailure \/ Terminal \/ ErrCard \/ ErrSend \/ TurnEnd
    \/ Drain \/ DrainEmpty \/ StopFinish \/ Unwind \/ Exit \/ Prologue \/ Finish1
    \/ Stutter

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ thr \in {"live", "deleted"} /\ known \in BOOLEAN
    /\ hp \in {"turn", "drain", "stopping", "unwind", "exit", "prologue", "final1", "done"}
    /\ tp \in {"none", "intent", "post", "run", "terminal", "err1", "err2", "end"}
    /\ cur \in Msgs \cup {"none"} /\ pe \in BOOLEAN /\ cardUp \in BOOLEAN /\ esc \in BOOLEAN
    /\ pending \in Seq(Msgs) /\ groups \in Seq(Msgs) /\ claiming \subseteq Msgs /\ limbo \subseteq Msgs
    /\ row \in [Msgs -> {"none", "pending", "finished"}]
    /\ processing \in BOOLEAN /\ parentPosts \in 0..1
    /\ bad \in BOOLEAN /\ badStart \in BOOLEAN /\ midStop \in BOOLEAN /\ finStop \in BOOLEAN

\* Invariant 4': once the turn's thread exists, nothing for the turn or its queue goes to the parent channel.
NoParentPostAfterThread == parentPosts = 0
\* The same, restricted to a turn whose status card was already up when the thread vanished: the
\* #809 path (card posted, progress edits fail, the terminal write raises).
NoParentPostAfterCard == ~(parentPosts > 0 /\ cardUp)
\* Invariant 7a: no queued turn begins after the adapter has seen the thread is gone.
NoTurnAfterGone == ~badStart
\* Invariant 7b: no write is issued to the thread after the adapter has seen it is gone.
NoWriteAfterGoneObserved == ~bad
\* Invariant 8: once the handler has returned and nothing is in flight, every admitted message's row is
\* finished, so #764 startup replay does not dispatch it again.
AdmissionsFinishedWhenIdle ==
    Quiescent => \A m \in Msgs : row[m] \in {"none", "finished"}

\* Witnesses: each must be violated by GoneDesign-shaped configs, showing the path is reachable.
WitnessNoMidGroupStop == ~midStop
WitnessNoFinishOnStop == ~finStop
=============================================================================
