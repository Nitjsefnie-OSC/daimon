----------------------------- MODULE GrantInstall -----------------------------
EXTENDS Naturals
CONSTANT Unsafe
VARIABLES installations, owner, observedA, observedB, doneA, doneB
vars == <<installations, owner, observedA, observedB, doneA, doneB>>

Init == /\ installations = {}
        /\ owner = "none"
        /\ observedA = FALSE /\ observedB = FALSE
        /\ doneA = FALSE /\ doneB = FALSE

\* stage_grant takes lock_agent_grants before validating and writing:
\* packages/core/daimon/core/stores/github_access.py:406-415,444-470.
LockA == /\ owner = "none" /\ ~doneA /\ owner' = "A"
         /\ UNCHANGED <<installations, observedA, observedB, doneA, doneB>>
LockB == /\ owner = "none" /\ ~doneB /\ owner' = "B"
         /\ UNCHANGED <<installations, observedA, observedB, doneA, doneB>>
\* Proposed removal of the different-installation guard at github_access.py:416-428.
WriteA == /\ owner = "A"
          /\ installations' = installations \cup {"A"}
          /\ owner' = "none" /\ doneA' = TRUE
          /\ UNCHANGED <<observedA, observedB, doneB>>
WriteB == /\ owner = "B"
          /\ installations' = installations \cup {"B"}
          /\ owner' = "none" /\ doneB' = TRUE
          /\ UNCHANGED <<observedA, observedB, doneA>>
\* Rejected read-then-write without the lock: a stale snapshot loses a grant.
UnsafeReadA == /\ Unsafe /\ ~observedA /\ installations = {}
               /\ observedA' = TRUE
               /\ UNCHANGED <<installations, owner, observedB, doneA, doneB>>
UnsafeReadB == /\ Unsafe /\ ~observedB /\ installations = {}
               /\ observedB' = TRUE
               /\ UNCHANGED <<installations, owner, observedA, doneA, doneB>>
UnsafeWriteA == /\ Unsafe /\ observedA /\ ~doneA
                /\ installations' = {"A"} /\ doneA' = TRUE
                /\ UNCHANGED <<owner, observedA, observedB, doneB>>
UnsafeWriteB == /\ Unsafe /\ observedB /\ ~doneB
                /\ installations' = {"B"} /\ doneB' = TRUE
                /\ UNCHANGED <<owner, observedA, observedB, doneA>>
Next == LockA \/ LockB \/ WriteA \/ WriteB
        \/ UnsafeReadA \/ UnsafeReadB \/ UnsafeWriteA \/ UnsafeWriteB
Spec == Init /\ [][Next]_vars
TypeOK == /\ installations \subseteq {"A","B"}
          /\ owner \in {"none","A","B"}
          /\ observedA \in BOOLEAN /\ observedB \in BOOLEAN
          /\ doneA \in BOOLEAN /\ doneB \in BOOLEAN
CompletedGrantsPersist == /\ (doneA => "A" \in installations)
                          /\ (doneB => "B" \in installations)
=============================================================================
