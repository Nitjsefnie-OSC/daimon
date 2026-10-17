------------------------------ MODULE LinkedPending ------------------------------
EXTENDS Naturals, FiniteSets

\* Pending chat grant requests that wait for a verified personal GitHub link.
\* When the link callback finishes, each callback invocation (a worker here)
\* takes the requester's requests, handles each one and answers in its thread.
\*
\* DeleteOnClaim:        take deletes the rows it returns (the code before the
\*                       claim lease).
\* AbortOnException:     an exception in one request ends the batch; the
\*                       requests after it get no outcome.
\* ResolveBeforeDelivery: the row is deleted as soon as the outcome is known,
\*                       before the result is posted.
\* IgnoreLease:          take returns rows another worker claimed with a lease
\*                       that has not lapsed.
\* CrashEnabled:         a worker can die mid-batch.
CONSTANTS DeleteOnClaim, AbortOnException, ResolveBeforeDelivery,
          IgnoreLease, CrashEnabled
ASSUME /\ DeleteOnClaim \in BOOLEAN /\ AbortOnException \in BOOLEAN
       /\ ResolveBeforeDelivery \in BOOLEAN /\ IgnoreLease \in BOOLEAN
       /\ CrashEnabled \in BOOLEAN

\* Request ids double as the take order: oldest expires_at first.
Requests == {1, 2}
Workers == {"w1", "w2"}
None == "none"

\* Database rows. row: unsaved | pending | gone. expired: expires_at <= now.
\* claimedBy / leaseLive: claimed_until, as the claimer and whether it is
\* still in the future.
VARIABLES row, expired, claimedBy, leaseLive
\* Worker memory, lost on a crash. step[w][r]: na | todo | outcome | sent |
\* resolvedFirst | finished | aborted.
VARIABLES wstate, step
\* Ghosts. reached: some worker computed an outcome (a grant result or a
\* failure line). attempted: some worker tried to post it. visible: a post
\* succeeded. processed: how many workers computed an outcome. failed: an
\* exception was caught. lapsed: a lease on the request ran out.
\* expiryDeleted: the row was removed because it expired.
VARIABLES reached, attempted, visible, processed, failed, lapsed,
          expiryDeleted

dbVars == <<row, expired, claimedBy, leaseLive>>
workVars == <<wstate, step>>
ghostVars == <<reached, attempted, visible, processed, failed, lapsed,
               expiryDeleted>>
vars == <<dbVars, workVars, ghostVars>>

Init ==
  /\ row = [r \in Requests |-> "unsaved"]
  /\ expired = [r \in Requests |-> FALSE]
  /\ claimedBy = [r \in Requests |-> None]
  /\ leaseLive = [r \in Requests |-> FALSE]
  /\ wstate = [w \in Workers |-> "idle"]
  /\ step = [w \in Workers |-> [r \in Requests |-> "na"]]
  /\ reached = [r \in Requests |-> FALSE]
  /\ attempted = [r \in Requests |-> FALSE]
  /\ visible = [r \in Requests |-> FALSE]
  /\ processed = [r \in Requests |-> 0]
  /\ failed = [r \in Requests |-> FALSE]
  /\ lapsed = [r \in Requests |-> FALSE]
  /\ expiryDeleted = [r \in Requests |-> FALSE]

\* The request is saved when Connect needs a link (save_pending).
Save(r) ==
  /\ row[r] = "unsaved"
  /\ row' = [row EXCEPT ![r] = "pending"]
  /\ UNCHANGED <<expired, claimedBy, leaseLive, workVars, ghostVars>>

\* expires_at passes (ten minutes after the save).
Expire(r) ==
  /\ row[r] = "pending" /\ ~expired[r]
  /\ expired' = [expired EXCEPT ![r] = TRUE]
  /\ UNCHANGED <<row, claimedBy, leaseLive, workVars, ghostVars>>

\* claimed_until passes (two minutes after the claim), whether or not the
\* claimer is alive.
LeaseLapse(r) ==
  /\ row[r] = "pending" /\ leaseLive[r]
  /\ leaseLive' = [leaseLive EXCEPT ![r] = FALSE]
  /\ lapsed' = [lapsed EXCEPT ![r] = TRUE]
  /\ UNCHANGED <<row, expired, claimedBy, workVars, reached, attempted,
                  visible, processed, failed, expiryDeleted>>

\* take(): claim unexpired rows whose lease is unset or lapsed, then delete the
\* expired rows. One call per worker, in one transaction.
Takeable(r) ==
  /\ row[r] = "pending" /\ ~expired[r]
  /\ (IgnoreLease \/ claimedBy[r] = None \/ ~leaseLive[r])
Stale(r) == row[r] = "pending" /\ expired[r]

Take(w) ==
  /\ wstate[w] = "idle"
  /\ wstate' = [wstate EXCEPT ![w] = "working"]
  /\ step' = [step EXCEPT ![w] =
       [r \in Requests |-> IF Takeable(r) THEN "todo" ELSE "na"]]
  /\ claimedBy' = [r \in Requests |->
       IF Takeable(r) THEN w ELSE claimedBy[r]]
  /\ leaseLive' = [r \in Requests |->
       IF Takeable(r) THEN TRUE ELSE leaseLive[r]]
  /\ row' = [r \in Requests |->
       IF Stale(r) \/ (DeleteOnClaim /\ Takeable(r)) THEN "gone" ELSE row[r]]
  /\ expiryDeleted' = [r \in Requests |->
       expiryDeleted[r] \/ Stale(r)]
  /\ UNCHANGED <<expired, reached, attempted, visible, processed, failed,
                  lapsed>>

\* A worker handles its requests one at a time, lowest id first.
Active(w) == {r \in Requests :
  step[w][r] \in {"todo", "outcome", "sent", "resolvedFirst"}}
Cur(w) == CHOOSE r \in Active(w) : \A s \in Active(w) : r <= s
Working(w) == wstate[w] = "working" /\ Active(w) # {}

\* The agent lookup, authority check and grant, with no exception.
ProcessOk(w) ==
  /\ Working(w) /\ step[w][Cur(w)] = "todo"
  /\ step' = [step EXCEPT ![w][Cur(w)] = "outcome"]
  /\ reached' = [reached EXCEPT ![Cur(w)] = TRUE]
  /\ processed' = [processed EXCEPT ![Cur(w)] = @ + 1]
  /\ UNCHANGED <<dbVars, wstate, attempted, visible, failed, lapsed,
                  expiryDeleted>>

\* Any exception while handling the request. The fixed code turns it into a
\* failure line for that thread and goes on. AbortOnException models a loop
\* that lets the exception end the batch.
ProcessFail(w) ==
  /\ Working(w) /\ step[w][Cur(w)] = "todo"
  /\ step' = [step EXCEPT ![w] =
       [r \in Requests |->
         IF r = Cur(w) THEN "outcome"
         ELSE IF AbortOnException /\ step[w][r] = "todo" THEN "aborted"
         ELSE step[w][r]]]
  /\ reached' = [reached EXCEPT ![Cur(w)] = TRUE]
  /\ processed' = [processed EXCEPT ![Cur(w)] = @ + 1]
  /\ failed' = [failed EXCEPT ![Cur(w)] = TRUE]
  /\ UNCHANGED <<dbVars, wstate, attempted, visible, lapsed, expiryDeleted>>

\* _deliver_result: post the result line. The post can fail; the code logs it.
Deliver(w, ok) ==
  /\ Working(w)
  /\ step[w][Cur(w)] = (IF ResolveBeforeDelivery THEN "resolvedFirst"
                        ELSE "outcome")
  /\ step' = [step EXCEPT ![w][Cur(w)] =
       IF ResolveBeforeDelivery THEN "finished" ELSE "sent"]
  /\ attempted' = [attempted EXCEPT ![Cur(w)] = TRUE]
  /\ visible' = [visible EXCEPT ![Cur(w)] = @ \/ ok]
  /\ UNCHANGED <<dbVars, wstate, reached, processed, failed, lapsed,
                  expiryDeleted>>

\* resolve(): delete the row. A row already deleted by expiry stays deleted.
Resolve(w) ==
  /\ Working(w)
  /\ step[w][Cur(w)] = (IF ResolveBeforeDelivery THEN "outcome" ELSE "sent")
  /\ step' = [step EXCEPT ![w][Cur(w)] =
       IF ResolveBeforeDelivery THEN "resolvedFirst" ELSE "finished"]
  /\ row' = [row EXCEPT ![Cur(w)] =
       IF @ = "pending" THEN "gone" ELSE @]
  /\ UNCHANGED <<expired, claimedBy, leaseLive, wstate, ghostVars>>

\* The process dies: its in-flight state is gone, the claims stay.
Crash(w) ==
  /\ CrashEnabled /\ Working(w)
  /\ wstate' = [wstate EXCEPT ![w] = "crashed"]
  /\ step' = [step EXCEPT ![w] = [r \in Requests |-> "na"]]
  /\ UNCHANGED <<dbVars, ghostVars>>

Next ==
  \/ \E r \in Requests: Save(r) \/ Expire(r) \/ LeaseLapse(r)
  \/ \E w \in Workers:
       Take(w) \/ ProcessOk(w) \/ ProcessFail(w) \/ Resolve(w) \/ Crash(w)
       \/ Deliver(w, TRUE) \/ Deliver(w, FALSE)

Spec == Init /\ [][Next]_vars

\* A row leaves the table only after its outcome exists and a post was
\* attempted, unless it expired.
NoSilentLoss ==
  \A r \in Requests :
    (row[r] = "gone" /\ ~expiryDeleted[r]) => (reached[r] /\ attempted[r])

\* Two workers compute an outcome for one request only after its lease has
\* lapsed. With a live lease, take skips the row.
NoDoubleProcessing ==
  \A r \in Requests : processed[r] <= 1 \/ lapsed[r]

\* Witnesses: each is violated on purpose to show the path is reachable.
RetakeAfterCrashNeverFinishes ==
  ~(wstate["w1"] = "crashed" /\ \E r \in Requests : step["w2"][r] = "finished")
FailureNeverUnblocksOthers ==
  ~(failed[1] /\ step["w1"][2] = "finished")
\* The accepted gap: a failed post is logged and not retried, so the row can be
\* deleted with no line in the thread.
DeliveryFailureAlwaysVisible ==
  \A r \in Requests :
    (row[r] = "gone" /\ ~expiryDeleted[r]) => visible[r]

WorkerStep ==
  \E w \in Workers :
    ProcessOk(w) \/ ProcessFail(w) \/ Resolve(w)
    \/ Deliver(w, TRUE) \/ Deliver(w, FALSE)

\* A request a live worker holds reaches an outcome even when another request
\* in its batch fails. Needs weak fairness on the worker's own steps and
\* CrashEnabled = FALSE.
Held(r) == \E w \in Workers : step[w][r] # "na"
HeldReachesOutcome == \A r \in Requests : Held(r) ~> reached[r]

LiveSpec == Spec /\ WF_vars(WorkerStep)
================================================================================
