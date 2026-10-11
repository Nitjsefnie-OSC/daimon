-------------------- MODULE TestSessionTaskLifecycle --------------------
EXTENDS Naturals, TLC

VARIABLES task, cleanupStarted
vars == <<task, cleanupStarted>>

Init ==
    /\ task = "none"
    /\ cleanupStarted = FALSE

Spawn ==
    /\ task = "none"
    /\ ~cleanupStarted
    /\ task' = "queued"
    /\ UNCHANGED cleanupStarted

BeginDbWork ==
    /\ task = "queued"
    /\ task' = "using_db"
    /\ UNCHANGED cleanupStarted

FinishTask ==
    /\ task \in {"queued", "using_db"}
    /\ task' = "done"
    /\ UNCHANGED cleanupStarted

UnsafeCleanup ==
    /\ ~cleanupStarted
    /\ cleanupStarted' = TRUE
    /\ UNCHANGED task

JoinedCleanup ==
    /\ ~cleanupStarted
    /\ task \in {"none", "done"}
    /\ cleanupStarted' = TRUE
    /\ UNCHANGED task

NoCleanupDuringTask == cleanupStarted => task \in {"none", "done"}

UnsafeNext == Spawn \/ BeginDbWork \/ FinishTask \/ UnsafeCleanup
SafeNext == Spawn \/ BeginDbWork \/ FinishTask \/ JoinedCleanup

UnsafeSpec == Init /\ [][UnsafeNext]_vars
SafeSpec == Init /\ [][SafeNext]_vars

=============================================================================
