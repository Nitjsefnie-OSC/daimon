------------------------- MODULE Reconcile -------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Repos, Workers, MaxEvents, NoGenerationCheck,
          LegacyOnlyWrite, NoLeaseOwnerCheck, NoSweep, OldDrainer
ASSUME Repos = {"a", "b"} /\ Workers = {1, 2} /\ MaxEvents = 2

VARIABLES exists, suspended, repos, version, events, deliveries, duplicated,
          receipts, generation, enqueuedVersion, job, owner, claimedGeneration,
          expired, workerState, workerGeneration, snapshotVersion,
          snapshotExists, snapshotSuspended, snapshotRepos,
          cacheExists, cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed,
          expirations, oldCode, refreshed
vars == <<exists, suspended, repos, version, events, deliveries, duplicated,
          receipts, generation, enqueuedVersion, job, owner, claimedGeneration,
          expired, workerState, workerGeneration, snapshotVersion,
          snapshotExists, snapshotSuspended, snapshotRepos,
          cacheExists, cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed,
          expirations, oldCode, refreshed>>
ext == <<oldCode, refreshed>>
Ids == 1..MaxEvents
Max(a, b) == IF a >= b THEN a ELSE b

Init ==
  /\ exists = TRUE /\ suspended = FALSE /\ repos = {}
  /\ version = 0 /\ events = [i \in Ids |-> 0]
  /\ deliveries = [i \in Ids |-> 0] /\ duplicated = {}
  /\ receipts = {}
  \* Migration 0108 queues one refresh per installation at migrate time.
  /\ generation = 1 /\ enqueuedVersion = 0
  /\ job = "pending" /\ owner = 0 /\ claimedGeneration = 0
  /\ oldCode = TRUE /\ refreshed = FALSE
  /\ expired = FALSE /\ expirations = 0
  /\ workerState = [w \in Workers |-> "idle"]
  /\ workerGeneration = [w \in Workers |-> 0]
  /\ snapshotVersion = [w \in Workers |-> 0]
  /\ snapshotExists = [w \in Workers |-> TRUE]
  /\ snapshotSuspended = [w \in Workers |-> FALSE]
  /\ snapshotRepos = [w \in Workers |-> {}]
  /\ cacheExists = TRUE /\ cacheSuspended = FALSE
  /\ cacheRepos = {} /\ cacheVersion = 0 /\ maxWrittenVersion = 0 /\ badOwnerWrite = FALSE
  /\ firstReceipt = 0 /\ duplicateProcessed = FALSE

GitHubChange(kind, repo) ==
  /\ version < MaxEvents
  /\ CASE kind = "add" -> exists /\ repo \in Repos /\ repo \notin repos
       [] kind = "remove" -> exists /\ repo \in repos
       [] kind = "suspend" -> exists /\ ~suspended
       [] kind = "unsuspend" -> exists /\ suspended
       [] kind = "delete" -> exists
  /\ version' = version + 1
  /\ events' = [events EXCEPT ![version + 1] = version + 1]
  /\ deliveries' = [deliveries EXCEPT ![version + 1] = 1]
  /\ exists' = IF kind = "delete" THEN FALSE ELSE exists
  /\ suspended' = IF kind = "suspend" THEN TRUE
                   ELSE IF kind \in {"unsuspend", "delete"} THEN FALSE
                   ELSE suspended
  /\ repos' = CASE kind = "add" -> repos \cup {repo}
                [] kind = "remove" -> repos \ {repo}
                [] kind = "delete" -> {}
                [] OTHER -> repos
  /\ UNCHANGED <<ext, duplicated, receipts, generation, enqueuedVersion,
                  job, owner, claimedGeneration, expired, workerState,
                  workerGeneration, snapshotVersion, snapshotExists,
                  snapshotSuspended, snapshotRepos, cacheExists,
                  cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed, expirations>>

Duplicate(i) ==
  /\ events[i] # 0 /\ i \notin duplicated
  /\ deliveries' = [deliveries EXCEPT ![i] = @ + 1]
  /\ duplicated' = duplicated \cup {i}
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, receipts,
                  generation, enqueuedVersion, job, owner, claimedGeneration,
                  expired, workerState, workerGeneration, snapshotVersion,
                  snapshotExists, snapshotSuspended, snapshotRepos,
                  cacheExists, cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed,
                  expirations>>

Enqueue(i) ==
  /\ deliveries[i] > 0
  /\ deliveries' = [deliveries EXCEPT ![i] = @ - 1]
  /\ IF i \in receipts THEN
       /\ duplicateProcessed' = TRUE
       /\ UNCHANGED <<receipts, generation, enqueuedVersion, job, firstReceipt>>
     ELSE
       /\ receipts' = receipts \cup {i}
       /\ firstReceipt' = IF firstReceipt = 0 THEN i ELSE firstReceipt
       /\ UNCHANGED duplicateProcessed
       /\ generation' = generation + 1
       /\ enqueuedVersion' = Max(enqueuedVersion, events[i])
       /\ job' = IF job = "running" THEN "running" ELSE "pending"
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, duplicated,
                  owner, claimedGeneration, expired, workerState,
                  workerGeneration, snapshotVersion, snapshotExists,
                  snapshotSuspended, snapshotRepos, cacheExists,
                  cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, expirations>>

Claim(w) ==
  /\ ~oldCode
  /\ workerState[w] = "idle"
  /\ job = "pending" \/ (job = "running" /\ expired)
  /\ job' = "running" /\ owner' = w
  /\ claimedGeneration' = generation /\ expired' = FALSE
  /\ workerState' = [workerState EXCEPT ![w] = "claimed"]
  /\ workerGeneration' = [workerGeneration EXCEPT ![w] = generation]
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, deliveries,
                  duplicated, receipts, generation, enqueuedVersion,
                  snapshotVersion, snapshotExists, snapshotSuspended,
                  snapshotRepos, cacheExists, cacheSuspended, cacheRepos,
                  cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed, expirations>>

ReadGitHub(w) ==
  /\ workerState[w] = "claimed"
  /\ workerState' = [workerState EXCEPT ![w] = "read"]
  /\ snapshotVersion' = [snapshotVersion EXCEPT ![w] = version]
  /\ snapshotExists' = [snapshotExists EXCEPT ![w] = exists]
  /\ snapshotSuspended' = [snapshotSuspended EXCEPT ![w] = suspended]
  /\ snapshotRepos' = [snapshotRepos EXCEPT ![w] = repos]
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, deliveries,
                  duplicated, receipts, generation, enqueuedVersion,
                  job, owner, claimedGeneration, expired, workerGeneration,
                  cacheExists, cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed,
                  expirations>>

LeaseExpire ==
  /\ job = "running" /\ ~expired /\ expirations = 0
  /\ expired' = TRUE /\ expirations' = 1
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, deliveries,
                  duplicated, receipts, generation, enqueuedVersion,
                  job, owner, claimedGeneration, workerState,
                  workerGeneration, snapshotVersion, snapshotExists,
                  snapshotSuspended, snapshotRepos, cacheExists,
                  cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed>>

Finish(w) ==
  /\ workerState[w] = "read"
  /\ workerState' = [workerState EXCEPT ![w] = "idle"]
  /\ IF job # "running" \/
         (~NoLeaseOwnerCheck /\ owner # w) \/
         claimedGeneration # workerGeneration[w]
     THEN UNCHANGED <<refreshed, job, owner, claimedGeneration, expired,
                       cacheExists, cacheSuspended, cacheRepos, cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed>>
     ELSE IF ~NoGenerationCheck /\ generation # workerGeneration[w]
       THEN /\ job' = "pending" /\ owner' = 0
            /\ claimedGeneration' = 0 /\ expired' = FALSE
            /\ UNCHANGED <<refreshed, cacheExists, cacheSuspended, cacheRepos,
                            cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed>>
       ELSE /\ job' = "done" /\ owner' = 0
            /\ claimedGeneration' = 0 /\ expired' = FALSE
            /\ IF LegacyOnlyWrite THEN
                 UNCHANGED <<refreshed, cacheExists, cacheSuspended, cacheRepos,
                             cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt, duplicateProcessed>>
               ELSE /\ refreshed' = TRUE
                    /\ cacheExists' = snapshotExists[w]
                    /\ cacheSuspended' = snapshotSuspended[w]
                    /\ cacheRepos' = snapshotRepos[w]
                    /\ cacheVersion' = snapshotVersion[w]
                    /\ maxWrittenVersion' = Max(maxWrittenVersion, snapshotVersion[w])
                    /\ badOwnerWrite' = (badOwnerWrite \/ (owner # w))
  /\ UNCHANGED <<oldCode, exists, suspended, repos, version, events, deliveries,
                  duplicated, receipts, generation, enqueuedVersion,
                  workerGeneration, snapshotVersion, snapshotExists,
                  snapshotSuspended, snapshotRepos, firstReceipt,
                  duplicateProcessed, expirations>>

\* Deploy order: migrations run while the previous image's scheduler is still
\* up. Its drain finishes the migration-queued job without writing the
\* snapshot columns it does not know. Only the migration's job (generation 1,
\* no webhook delivery yet) is modelled; a delivery consumed this way would
\* also be healed by the sweep because it leaves `refreshed` false.
OldDrain ==
  /\ OldDrainer /\ oldCode
  /\ job = "pending" /\ generation = 1
  /\ job' = "done"
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, deliveries,
                  duplicated, receipts, generation, enqueuedVersion, owner,
                  claimedGeneration, expired, workerState, workerGeneration,
                  snapshotVersion, snapshotExists, snapshotSuspended,
                  snapshotRepos, cacheExists, cacheSuspended, cacheRepos,
                  cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt,
                  duplicateProcessed, expirations>>

\* The new scheduler starts; the previous one is gone.
NewCodeStart ==
  /\ oldCode /\ oldCode' = FALSE
  /\ UNCHANGED <<refreshed, exists, suspended, repos, version, events,
                  deliveries, duplicated, receipts, generation,
                  enqueuedVersion, job, owner, claimedGeneration, expired,
                  workerState, workerGeneration, snapshotVersion,
                  snapshotExists, snapshotSuspended, snapshotRepos,
                  cacheExists, cacheSuspended, cacheRepos, cacheVersion,
                  maxWrittenVersion, badOwnerWrite, firstReceipt,
                  duplicateProcessed, expirations>>

\* Start of each drain tick in new code: queue a refresh for an installation
\* with no snapshot and no pending or running job. A done job does not count.
Sweep ==
  /\ ~NoSweep /\ ~oldCode /\ ~refreshed
  /\ job \in {"none", "done"}
  /\ generation' = generation + 1
  /\ job' = "pending"
  /\ UNCHANGED <<ext, exists, suspended, repos, version, events, deliveries,
                  duplicated, receipts, enqueuedVersion, owner,
                  claimedGeneration, expired, workerState, workerGeneration,
                  snapshotVersion, snapshotExists, snapshotSuspended,
                  snapshotRepos, cacheExists, cacheSuspended, cacheRepos,
                  cacheVersion, maxWrittenVersion, badOwnerWrite, firstReceipt,
                  duplicateProcessed, expirations>>

Next ==
  \/ \E kind \in {"add", "remove", "suspend", "unsuspend", "delete"},
         repo \in Repos : GitHubChange(kind, repo)
  \/ \E i \in Ids : Duplicate(i) \/ Enqueue(i)
  \/ \E w \in Workers : Claim(w) \/ ReadGitHub(w) \/ Finish(w)
  \/ LeaseExpire
  \/ OldDrain \/ NewCodeStart \/ Sweep

Fair == /\ \A i \in Ids : WF_vars(Enqueue(i))
        /\ \A w \in Workers : WF_vars(Claim(w)) /\ WF_vars(ReadGitHub(w))
                              /\ WF_vars(Finish(w))
        /\ WF_vars(NewCodeStart) /\ WF_vars(Sweep)
Spec == Init /\ [][Next]_vars /\ Fair

NoStaleOverwrite == ~badOwnerWrite /\ cacheVersion = maxWrittenVersion /\
                    (job # "done" \/ cacheVersion >= enqueuedVersion)
CacheMatches == cacheExists = exists /\ cacheSuspended = suspended
                /\ cacheRepos = repos
NoReorderDuplicateWitness == ~(firstReceipt = 2 /\ duplicateProcessed /\
  deliveries[1] = 0 /\ deliveries[2] = 0 /\ job = "done" /\ CacheMatches)
EventuallyConsistent == <>[]CacheMatches
\* Every installation eventually has a GitHub snapshot (or removal marker).
\* Relies on weak fairness for NewCodeStart, Sweep, Enqueue, Claim,
\* ReadGitHub and Finish; none for GitHub changes, duplicates, lease expiry or
\* the old drainer. Lease expiry is never forced, so a stranded job is only
\* recovered by Claim taking over an expired lease.
EventuallyRefreshed == <>refreshed

=============================================================================
