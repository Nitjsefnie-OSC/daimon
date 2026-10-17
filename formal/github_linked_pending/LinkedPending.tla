------------------------------ MODULE LinkedPending ------------------------------
EXTENDS Naturals, FiniteSets

\* Pending chat grant requests that wait for a verified personal GitHub link.
\* When the link callback finishes, each callback invocation (a worker here)
\* takes the requester's requests, handles each one and answers in its thread.
\* The callback runs inline in the browser request, so a worker that dies (or
\* lets an exception escape) shows the person a failed browser page.
\*
\* DeleteOnClaim:         take deletes the rows it returns and writes no claim,
\*                        and the loop has no per-request renewal (the code
\*                        before the claim lease).
\* AbortOnException:      an exception in one request leaves the loop: the
\*                        failing request gets no outcome and the rest of the
\*                        batch is abandoned. With DeleteOnClaim this is the
\*                        old loop exactly.
\* ResolveBeforeDelivery: the row is deleted as soon as the outcome is known,
\*                        before the result is posted.
\* IgnoreLease:           take returns rows another worker claimed with a lease
\*                        that has not lapsed.
\* RenewChecksToken:      the renewal before each request is conditional on the
\*                        worker's claim token (off = renewal by id only).
\* ResolveChecksToken:    resolve deletes only on id and token (off = by id).
\* AtomicRecovery:        _deliver_recovery saves the replacement row and
\*                        resolves the original in one transaction (off = the
\*                        replacement commits first and the original is deleted
\*                        by the loop's final resolve).
\* ExpiryCleanup:         which expired rows take deletes.
\*   "grace"      expired and (never claimed or lease lapsed Garbage ticks
\*                ago): CLAIMED_GARBAGE_AFTER, the design.
\*   "lapsed"     expired and (never claimed or lease lapsed): no grace.
\*   "all"        every expired row (the code at 9441d430d).
\* Heartbeat:             before each request the callback renews the lease on
\*                        every row it still holds, by claim token (off = only
\*                        the current request's row).
\* WithDeadline:          each request is bounded by PER_REQUEST_DEADLINE: a
\*                        request still processing at the deadline ends with a
\*                        failure line (off = a request can take any time).
\* RenewException:        the renewal call itself can raise; the callback logs
\*                        it and skips that request, leaving its row claimed.
\* CrashEnabled:          a worker can die mid-batch.
\*
\* Time is counted in ticks, the same for every row and worker, with
\* Lease < Deadline and Deadline + Lease < Garbage (code: CLAIM_LEASE 2 min,
\* PER_REQUEST_DEADLINE 5 min, CLAIMED_GARBAGE_AFTER 60 min). A row's age is
\* the ticks since claimed_until was last set; its lease lapses at age Lease
\* and cleanup may delete it at age Lease + Garbage. A live worker never lets
\* a request run past Deadline ticks (the tick action is disabled for it),
\* the one timing assumption, which WithDeadline = FALSE removes. Expiry is
\* not counted: expires_at can pass at any time.
CONSTANTS DeleteOnClaim, AbortOnException, ResolveBeforeDelivery,
          IgnoreLease, RenewChecksToken, ResolveChecksToken, AtomicRecovery,
          ExpiryCleanup, CrashEnabled, Heartbeat, WithDeadline,
          RenewException, Lease, Deadline, Garbage
ASSUME /\ DeleteOnClaim \in BOOLEAN /\ AbortOnException \in BOOLEAN
       /\ ResolveBeforeDelivery \in BOOLEAN /\ IgnoreLease \in BOOLEAN
       /\ RenewChecksToken \in BOOLEAN /\ ResolveChecksToken \in BOOLEAN
       /\ AtomicRecovery \in BOOLEAN /\ CrashEnabled \in BOOLEAN
       /\ ExpiryCleanup \in {"grace", "lapsed", "all"}
       /\ Heartbeat \in BOOLEAN /\ WithDeadline \in BOOLEAN
       /\ RenewException \in BOOLEAN
       /\ Lease \in Nat /\ Deadline \in Nat /\ Garbage \in Nat
       /\ Lease < Deadline /\ Deadline + Lease < Garbage

\* Request ids double as the take order: oldest expires_at first.
Requests == {1, 2}
Stale == Lease + Garbage
Workers == {"w1", "w2"}
None == "none"

\* Database rows. row: unsaved | pending | gone. expired: expires_at <= now.
\* claimedBy: the claim token (a worker's one take is one fresh token), None
\* while claimed_until is null. age: ticks since claimed_until was last set
\* (0 for an unclaimed or deleted row), saturating at Stale.
VARIABLES row, expired, claimedBy, age
\* Worker memory, lost on a crash. step[w][r]: na | todo | started |
\* needRecovery | recovered | outcome | sent | resolvedFirst | finished |
\* skipped | aborted. win[w]: ticks since the worker's last renewal or take.
VARIABLES wstate, step, win
\* Ghosts, written by actions and never read by them.
\*   reached        some worker computed an outcome (a grant result, a recovery
\*                  decision or a failure line).
\*   attempted      some worker tried to post to the person (private recovery
\*                  link or thread line).
\*   visible        a thread post succeeded.   delivered  thread posts tried.
\*   failed         an exception was caught, or a request hit its deadline.
\*   lapseExcuse    a lease lapsed while a live worker was past its renewal
\*                  for the request, or after a post for it was tried (the
\*                  worker may have died before its resolve): a second take
\*                  and a second post are then possible and accepted.
\*   cleanedUnheld  the row was deleted as expired while no live worker held
\*                  it unstarted or in flight (never claimed, or its holder
\*                  died, skipped it or finished with it).
\*   crashVisible   a worker that held the request unfinished died or let an
\*                  exception escape: the person saw a failed browser request.
\*   crashThenDone  the request reached an outcome and a post after that.
\*   replSaved      a replacement pending row was saved for the request.
\*   recov          the outcome asked for recovery (a fresh link).
VARIABLES reached, attempted, visible, delivered, failed, lapseExcuse,
          cleanedUnheld, crashVisible, crashThenDone, replSaved, recov

dbVars == <<row, expired, claimedBy, age>>
workVars == <<wstate, step, win>>
ghostVars == <<reached, attempted, visible, delivered, failed, lapseExcuse,
               cleanedUnheld, crashVisible, crashThenDone, replSaved, recov>>
vars == <<dbVars, workVars, ghostVars>>

Min(a, b) == IF a < b THEN a ELSE b

Init ==
  /\ row = [r \in Requests |-> "unsaved"]
  /\ expired = [r \in Requests |-> FALSE]
  /\ claimedBy = [r \in Requests |-> None]
  /\ age = [r \in Requests |-> 0]
  /\ wstate = [w \in Workers |-> "idle"]
  /\ step = [w \in Workers |-> [r \in Requests |-> "na"]]
  /\ win = [w \in Workers |-> 0]
  /\ reached = [r \in Requests |-> FALSE]
  /\ attempted = [r \in Requests |-> FALSE]
  /\ visible = [r \in Requests |-> FALSE]
  /\ delivered = [r \in Requests |-> 0]
  /\ failed = [r \in Requests |-> FALSE]
  /\ lapseExcuse = [r \in Requests |-> FALSE]
  /\ cleanedUnheld = [r \in Requests |-> FALSE]
  /\ crashVisible = [r \in Requests |-> FALSE]
  /\ crashThenDone = [r \in Requests |-> FALSE]
  /\ replSaved = [r \in Requests |-> FALSE]
  /\ recov = [r \in Requests |-> FALSE]

\* A worker is past its renewal for r (or, in the legacy loop, holds r at all).
Flying == {"started", "needRecovery", "recovered", "outcome", "sent",
           "resolvedFirst"}
InFlight(w, r) ==
  \/ step[w][r] \in Flying
  \/ (DeleteOnClaim /\ step[w][r] = "todo")
InFlightAny(r) == \E w \in Workers : InFlight(w, r)
\* A live worker still intends to run or finish r.
HeldLive(r) ==
  \E w \in Workers :
    /\ wstate[w] = "working"
    /\ (step[w][r] \in Flying \/ step[w][r] = "todo")

\* claimed_until in the future / in the past.
LeaseLive(r) == claimedBy[r] # None /\ age[r] < Lease
Lapsed(r) == claimedBy[r] # None /\ age[r] >= Lease

\* The request is saved when Connect needs a link (save_pending).
Save(r) ==
  /\ row[r] = "unsaved"
  /\ row' = [row EXCEPT ![r] = "pending"]
  /\ UNCHANGED <<expired, claimedBy, age, workVars, ghostVars>>

\* expires_at passes (ten minutes after the save).
Expire(r) ==
  /\ row[r] = "pending" /\ ~expired[r]
  /\ expired' = [expired EXCEPT ![r] = TRUE]
  /\ UNCHANGED <<row, claimedBy, age, workVars, ghostVars>>

\* A worker handles its requests one at a time, lowest id first.
Active(w) == {r \in Requests : step[w][r] \in Flying \cup {"todo"}}
Cur(w) == CHOOSE r \in Active(w) : \A s \in Active(w) : r <= s
Working(w) == wstate[w] = "working" /\ Active(w) # {}

\* Time passes by one tick for every claim. No time passes while a worker has a
\* renewal pending (the take, the renewal and the start of the next request
\* follow each other with only database calls between them), and a worker with a
\* deadline cannot let a request run past Deadline ticks: the tick is disabled
\* until the worker acts. Without a deadline a request can take any time.
RenewPending(w) == Working(w) /\ step[w][Cur(w)] = "todo"
InRequest(w) == Working(w) /\ step[w][Cur(w)] \in Flying
Tick ==
  /\ \A w \in Workers :
       /\ ~RenewPending(w)
       /\ (WithDeadline /\ InRequest(w)) => win[w] < Deadline
  /\ age' = [r \in Requests |->
       IF row[r] = "pending" /\ claimedBy[r] # None
         THEN Min(age[r] + 1, Stale) ELSE 0]
  /\ win' = [w \in Workers |->
       IF WithDeadline /\ InRequest(w) THEN win[w] + 1 ELSE win[w]]
  /\ lapseExcuse' = [r \in Requests |->
       lapseExcuse[r]
       \/ (row[r] = "pending" /\ LeaseLive(r) /\ age[r] + 1 >= Lease
           /\ (InFlightAny(r) \/ delivered[r] > 0))]
  /\ UNCHANGED <<row, expired, claimedBy, wstate, step, reached, attempted,
                  visible, delivered, failed, cleanedUnheld, crashVisible,
                  crashThenDone, replSaved, recov>>

\* take(): claim unexpired rows whose lease is unset or lapsed with a fresh
\* token, then delete expired rows by the ExpiryCleanup rule. One call per
\* worker, in one transaction.
Takeable(r) ==
  /\ row[r] = "pending" /\ ~expired[r]
  /\ (IgnoreLease \/ claimedBy[r] = None \/ Lapsed(r))
Cleanable(r) ==
  /\ row[r] = "pending" /\ expired[r]
  /\ \/ ExpiryCleanup = "all"
     \/ claimedBy[r] = None
     \/ (ExpiryCleanup = "lapsed" /\ Lapsed(r))
     \/ (ExpiryCleanup = "grace" /\ age[r] >= Stale)

Take(w) ==
  /\ wstate[w] = "idle"
  /\ wstate' = [wstate EXCEPT ![w] = "working"]
  /\ step' = [step EXCEPT ![w] =
       [r \in Requests |-> IF Takeable(r) THEN "todo" ELSE "na"]]
  /\ win' = [win EXCEPT ![w] = 0]
  /\ claimedBy' = [r \in Requests |->
       IF Takeable(r) /\ ~DeleteOnClaim THEN w ELSE claimedBy[r]]
  /\ age' = [r \in Requests |->
       IF Takeable(r) \/ Cleanable(r) THEN 0 ELSE age[r]]
  /\ row' = [r \in Requests |->
       IF Cleanable(r) \/ (DeleteOnClaim /\ Takeable(r)) THEN "gone"
       ELSE row[r]]
  /\ cleanedUnheld' = [r \in Requests |->
       cleanedUnheld[r] \/ (Cleanable(r) /\ ~HeldLive(r))]
  /\ UNCHANGED <<expired, reached, attempted, visible, delivered, failed,
                  lapseExcuse, crashVisible, crashThenDone, replSaved, recov>>

\* Before each request: renew claimed_until, conditional on the worker's token.
\* The heartbeat (renew_claim) renews, in one statement, every row that still
\* carries the worker's token (rows it has not started, the current one and
\* rows it skipped); without it (the old renew) only the current row. A current
\* row that is gone or carries another token was lost to another callback: skip
\* it. The renewal call itself can raise: the request is skipped and nothing is
\* renewed. The legacy loop (DeleteOnClaim) has no renewal. Renewal by id alone
\* (RenewChecksToken = FALSE) is the code before the claim token.
RenewOk(w, r) ==
  /\ row[r] = "pending"
  /\ (RenewChecksToken => claimedBy[r] = w)
Renewed(w) ==
  IF Heartbeat /\ RenewChecksToken
    THEN {r \in Requests : row[r] = "pending" /\ claimedBy[r] = w}
    ELSE {Cur(w)}

Start(w) ==
  /\ Working(w) /\ step[w][Cur(w)] = "todo"
  /\ \/ /\ DeleteOnClaim
        /\ step' = [step EXCEPT ![w][Cur(w)] = "started"]
        /\ UNCHANGED <<age, win>>
     \/ /\ ~DeleteOnClaim /\ RenewException
        /\ step' = [step EXCEPT ![w][Cur(w)] = "skipped"]
        /\ UNCHANGED <<age, win>>
     \/ /\ ~DeleteOnClaim
        /\ age' = [r \in Requests |->
             IF r \in Renewed(w) /\ RenewOk(w, r) THEN 0 ELSE age[r]]
        /\ win' = [win EXCEPT ![w] = 0]
        /\ step' = [step EXCEPT ![w][Cur(w)] =
             IF RenewOk(w, Cur(w)) THEN "started" ELSE "skipped"]
  /\ UNCHANGED <<row, expired, claimedBy, wstate, ghostVars>>

\* The agent lookup, authority check and grant, no exception, nothing to
\* recover.
ProcessOk(w) ==
  /\ Working(w) /\ step[w][Cur(w)] = "started"
  /\ step' = [step EXCEPT ![w][Cur(w)] = "outcome"]
  /\ reached' = [reached EXCEPT ![Cur(w)] = TRUE]
  /\ UNCHANGED <<dbVars, wstate, win, attempted, visible, delivered, failed,
                  lapseExcuse, cleanedUnheld, crashVisible, crashThenDone,
                  replSaved, recov>>

\* The grant needs a fresh link (needs_link, relink): _deliver_recovery runs.
\* Not explored for the legacy loop (DeleteOnClaim), which has no claim for the
\* recovery transaction to resolve.
ProcessRecovery(w) ==
  /\ ~DeleteOnClaim
  /\ Working(w) /\ step[w][Cur(w)] = "started"
  /\ step' = [step EXCEPT ![w][Cur(w)] = "needRecovery"]
  /\ reached' = [reached EXCEPT ![Cur(w)] = TRUE]
  /\ recov' = [recov EXCEPT ![Cur(w)] = TRUE]
  /\ UNCHANGED <<dbVars, wstate, win, attempted, visible, delivered, failed,
                  lapseExcuse, cleanedUnheld, crashVisible, crashThenDone,
                  replSaved>>

\* Any exception while handling the request. The fixed code turns it into a
\* failure line for that thread and goes on. AbortOnException models the old
\* loop: the exception leaves it, the failing request has no outcome, the rest
\* of the batch is abandoned, and the callback fails in the browser.
ProcessFail(w) ==
  /\ Working(w) /\ step[w][Cur(w)] = "started"
  /\ IF AbortOnException
       THEN /\ wstate' = [wstate EXCEPT ![w] = "crashed"]
            /\ step' = [step EXCEPT ![w] =
                 [r \in Requests |->
                   IF r \in Active(w) THEN "aborted" ELSE step[w][r]]]
            /\ crashVisible' = [r \in Requests |->
                 crashVisible[r] \/ r \in Active(w)]
            /\ UNCHANGED <<reached, failed>>
       ELSE /\ step' = [step EXCEPT ![w][Cur(w)] = "outcome"]
            /\ reached' = [reached EXCEPT ![Cur(w)] = TRUE]
            /\ failed' = [failed EXCEPT ![Cur(w)] = TRUE]
            /\ UNCHANGED <<wstate, crashVisible>>
  /\ UNCHANGED <<dbVars, win, attempted, visible, delivered, lapseExcuse,
                  cleanedUnheld, crashThenDone, replSaved, recov>>

\* The per-request deadline (asyncio.timeout around the outcome and its result
\* post): a request still processing when its window is used up is cancelled
\* and ends with a failure line, like an exception. A deadline that fires while
\* the result is being posted is the Deliver action itself. The failure line
\* has its own short deadline and the resolve follows at once: both take no
\* ticks here, so Deadline stands for the request deadline plus the failure
\* line deadline.
Timeout(w) ==
  /\ WithDeadline
  /\ Working(w) /\ win[w] >= Deadline
  /\ step[w][Cur(w)] \in {"started", "needRecovery", "recovered"}
  /\ step' = [step EXCEPT ![w][Cur(w)] = "outcome"]
  /\ reached' = [reached EXCEPT ![Cur(w)] = TRUE]
  /\ failed' = [failed EXCEPT ![Cur(w)] = TRUE]
  /\ UNCHANGED <<dbVars, wstate, win, attempted, visible, delivered,
                  lapseExcuse, cleanedUnheld, crashVisible, crashThenDone,
                  replSaved, recov>>

\* _deliver_recovery, atomic design: one transaction saves the replacement row
\* and resolves the original. The resolve must match (id, and the token when
\* checked); if it deletes nothing the transaction rolls back and the worker
\* skips, because another worker owns the request.
RecoveryTx(w) ==
  /\ AtomicRecovery
  /\ Working(w) /\ step[w][Cur(w)] = "needRecovery"
  /\ IF row[Cur(w)] = "pending"
          /\ (ResolveChecksToken => claimedBy[Cur(w)] = w)
       THEN /\ row' = [row EXCEPT ![Cur(w)] = "gone"]
            /\ age' = [age EXCEPT ![Cur(w)] = 0]
            /\ replSaved' = [replSaved EXCEPT ![Cur(w)] = TRUE]
            /\ step' = [step EXCEPT ![w][Cur(w)] = "recovered"]
       ELSE /\ step' = [step EXCEPT ![w][Cur(w)] = "skipped"]
            /\ UNCHANGED <<row, age, replSaved>>
  /\ UNCHANGED <<expired, claimedBy, wstate, win, reached, attempted,
                  visible, delivered, failed, lapseExcuse, cleanedUnheld,
                  crashVisible, crashThenDone, recov>>

\* _deliver_recovery at 9441d430d: the replacement row commits on its own; the
\* original stays until the loop's final resolve.
SaveReplacement(w) ==
  /\ ~AtomicRecovery
  /\ Working(w) /\ step[w][Cur(w)] = "needRecovery"
  /\ step' = [step EXCEPT ![w][Cur(w)] = "recovered"]
  /\ replSaved' = [replSaved EXCEPT ![Cur(w)] = TRUE]
  /\ UNCHANGED <<dbVars, wstate, win, reached, attempted, visible, delivered,
                  failed, lapseExcuse, cleanedUnheld, crashVisible,
                  crashThenDone, recov>>

\* _post_personal_link: the private delivery attempt. A failure only changes
\* the thread line, which the model does not distinguish.
PrivateDeliver(w) ==
  /\ Working(w) /\ step[w][Cur(w)] = "recovered"
  /\ step' = [step EXCEPT ![w][Cur(w)] = "outcome"]
  /\ attempted' = [attempted EXCEPT ![Cur(w)] = TRUE]
  /\ UNCHANGED <<dbVars, wstate, win, reached, visible, delivered, failed,
                  lapseExcuse, cleanedUnheld, crashVisible, crashThenDone,
                  replSaved, recov>>

\* _deliver_result: post the result line. The post can fail; the code logs it.
Deliver(w, ok) ==
  /\ Working(w)
  /\ step[w][Cur(w)] = (IF ResolveBeforeDelivery THEN "resolvedFirst"
                        ELSE "outcome")
  /\ step' = [step EXCEPT ![w][Cur(w)] =
       IF ResolveBeforeDelivery THEN "finished" ELSE "sent"]
  /\ attempted' = [attempted EXCEPT ![Cur(w)] = TRUE]
  /\ visible' = [visible EXCEPT ![Cur(w)] = @ \/ ok]
  /\ delivered' = [delivered EXCEPT ![Cur(w)] = @ + 1]
  /\ crashThenDone' = [crashThenDone EXCEPT ![Cur(w)] =
       @ \/ crashVisible[Cur(w)]]
  /\ UNCHANGED <<dbVars, wstate, win, reached, failed, lapseExcuse,
                  cleanedUnheld, crashVisible, replSaved, recov>>

\* resolve(): delete the row on id (and token when checked). A row that is
\* already gone, or claimed by another worker, is left alone.
Resolve(w) ==
  /\ Working(w)
  /\ step[w][Cur(w)] = (IF ResolveBeforeDelivery THEN "outcome" ELSE "sent")
  /\ step' = [step EXCEPT ![w][Cur(w)] =
       IF ResolveBeforeDelivery THEN "resolvedFirst" ELSE "finished"]
  /\ row' = [row EXCEPT ![Cur(w)] =
       IF @ = "pending" /\ (ResolveChecksToken => claimedBy[Cur(w)] = w)
         THEN "gone" ELSE @]
  /\ age' = [age EXCEPT ![Cur(w)] =
       IF row[Cur(w)] = "pending"
          /\ (ResolveChecksToken => claimedBy[Cur(w)] = w)
         THEN 0 ELSE @]
  /\ UNCHANGED <<expired, claimedBy, wstate, win, ghostVars>>

\* The process dies: its in-flight state is gone, the claims stay, and the
\* browser request shows a failure for every request it still held.
Crash(w) ==
  /\ CrashEnabled /\ Working(w)
  /\ wstate' = [wstate EXCEPT ![w] = "crashed"]
  /\ step' = [step EXCEPT ![w] = [r \in Requests |-> "na"]]
  /\ crashVisible' = [r \in Requests |->
       crashVisible[r] \/ r \in Active(w)]
  /\ UNCHANGED <<dbVars, win, reached, attempted, visible, delivered, failed,
                  lapseExcuse, cleanedUnheld, crashThenDone, replSaved,
                  recov>>

Next ==
  \/ Tick
  \/ \E r \in Requests: Save(r) \/ Expire(r)
  \/ \E w \in Workers:
       Take(w) \/ Start(w) \/ ProcessOk(w) \/ ProcessRecovery(w)
       \/ ProcessFail(w) \/ Timeout(w) \/ RecoveryTx(w) \/ SaveReplacement(w)
       \/ PrivateDeliver(w) \/ Resolve(w) \/ Crash(w)
       \/ Deliver(w, TRUE) \/ Deliver(w, FALSE)

Spec == Init /\ [][Next]_vars

\* A row is never removed unless one of these holds:
\*   - its request reached an outcome and a post to the person was attempted;
\*   - it expired and no live worker still intended to run it (never claimed,
\*     or its holder died, skipped it or finished with it): cleanedUnheld;
\*   - a worker that held it unfinished died or let an exception escape (the
\*     person saw a failed browser request);
\*   - the recovery transaction replaced it with a fresh pending row;
\*   - a live worker is past its renewal for it and has not posted yet (the
\*     state form of "will attempt": see HeldReachesOutcome and
\*     OutcomeReachesAttempt in live.cfg).
\* A row whose holder is alive but has not yet started it (step todo) is not
\* covered: the renewal would fail and the worker would skip it.
NoSilentLoss ==
  \A r \in Requests :
    row[r] = "gone" =>
      \/ attempted[r]
      \/ cleanedUnheld[r]
      \/ crashVisible[r]
      \/ replSaved[r]
      \/ InFlightAny(r)

\* Two thread posts for one request only if its lease lapsed while a live
\* worker was working on it or after a post was tried (a crash or a slow
\* resolve; accepted). A stale worker whose token no longer matches skips, and
\* a second take never returns a live claim.
NoDuplicateDelivery ==
  \A r \in Requests : delivered[r] <= 1 \/ lapseExcuse[r]

\* A worker's death or escaped exception never deletes the request it held:
\* until the request expires, the row stays for a later take, or the recovery
\* transaction replaced it, or it was answered.
CrashLeavesRetakeable ==
  \A r \in Requests :
    (crashVisible[r] /\ ~attempted[r] /\ ~expired[r]) =>
      (row[r] = "pending" \/ replSaved[r])

\* Recovery atomicity: the original and its replacement are never both
\* present, and the original is never gone with no replacement and no post,
\* unless it expired and was cleaned after its holder left it.
RecoveryAtomic ==
  \A r \in Requests :
    /\ ~(replSaved[r] /\ row[r] = "pending")
    /\ (recov[r] /\ row[r] = "gone" /\ ~replSaved[r]) =>
       (attempted[r] \/ cleanedUnheld[r])

\* Witnesses: each is violated on purpose to show the path is reachable.
\* The request a worker held when it died is later answered (outcome and a
\* post) by a second take.
RetakeAfterCrashNeverFinishes == ~(\E r \in Requests : crashThenDone[r])
FailureNeverUnblocksOthers ==
  ~(failed[1] /\ step["w1"][2] = "finished")
\* The accepted gap: a failed post is logged and not retried, so the row can be
\* deleted with no line in the thread.
DeliveryFailureAlwaysVisible ==
  ~(\E w \in Workers, r \in Requests :
      step[w][r] = "finished" /\ ~visible[r])
RecoveryNeverCommits == ~(\E r \in Requests : replSaved[r] /\ row[r] = "gone")
SkipNeverHappens ==
  ~(\E w \in Workers, r \in Requests : step[w][r] = "skipped")
\* A request that hit its deadline ends with a failure line in its thread.
DeadlineNeverFires ==
  ~(\E w \in Workers, r \in Requests :
      failed[r] /\ step[w][r] = "finished" /\ visible[r])
\* A renewal exception skips a request while its row is still claimed by the
\* skipping worker.
RenewExceptionNeverSkips ==
  ~(\E w \in Workers, r \in Requests :
      step[w][r] = "skipped" /\ row[r] = "pending" /\ claimedBy[r] = w)
\* A claimed row survives past its lease: it is deleted only at age Stale.
GraceNeverCleans ==
  ~(\E r \in Requests : cleanedUnheld[r] /\ claimedBy[r] # None)

WorkerStep ==
  \E w \in Workers :
    Start(w) \/ ProcessOk(w) \/ ProcessRecovery(w) \/ ProcessFail(w)
    \/ Timeout(w) \/ RecoveryTx(w) \/ SaveReplacement(w) \/ PrivateDeliver(w)
    \/ Resolve(w) \/ Deliver(w, TRUE) \/ Deliver(w, FALSE)

\* A request some worker holds reaches an outcome (a grant result, a recovery
\* decision or a failure line) even when another request in its batch fails,
\* unless its holder skips it (lost claim or renewal exception).
\* Weak fairness on the workers' own steps; none on Save, Take, Expire or
\* Tick. CrashEnabled = FALSE: a dead worker holds nothing.
Held(r) == \E w \in Workers : step[w][r] \in {"todo"} \cup Flying
Skipped(r) == \E w \in Workers : step[w][r] = "skipped"
HeldReachesOutcome == \A r \in Requests : Held(r) ~> (reached[r] \/ Skipped(r))
\* An outcome is followed by a post attempt, unless a worker skipped the
\* request afterwards (its claim passed to another callback, or a renewal
\* exception); a skipped request stays claimed for a later callback.
OutcomeReachesAttempt ==
  \A r \in Requests : reached[r] ~> (attempted[r] \/ Skipped(r))

LiveSpec == Spec /\ WF_vars(WorkerStep)
================================================================================
