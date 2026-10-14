----------------------------- MODULE GrantInstall -----------------------------
EXTENDS Naturals, FiniteSets
CONSTANT Unsafe
VARIABLES installations, owner, observedA, observedB, doneA, doneB
vars == <<installations, owner, observedA, observedB, doneA, doneB>>

Init == /\ installations = {}
        /\ owner = "none"
        /\ observedA = FALSE
        /\ observedB = FALSE
        /\ doneA = FALSE
        /\ doneB = FALSE

LockA == /\ owner = "none" /\ ~doneA /\ owner' = "A"
         /\ UNCHANGED <<installations, observedA, observedB, doneA, doneB>>
LockB == /\ owner = "none" /\ ~doneB /\ owner' = "B"
         /\ UNCHANGED <<installations, observedA, observedB, doneA, doneB>>
WriteA == /\ owner = "A" /\ installations \subseteq {"A"}
          /\ installations' = installations \cup {"A"}
          /\ owner' = "none" /\ doneA' = TRUE
          /\ UNCHANGED <<observedA, observedB, doneB>>
WriteB == /\ owner = "B" /\ installations \subseteq {"B"}
          /\ installations' = installations \cup {"B"}
          /\ owner' = "none" /\ doneB' = TRUE
          /\ UNCHANGED <<observedA, observedB, doneA>>
RejectA == /\ owner = "A" /\ installations = {"B"}
           /\ owner' = "none" /\ doneA' = TRUE
           /\ UNCHANGED <<installations, observedA, observedB, doneB>>
RejectB == /\ owner = "B" /\ installations = {"A"}
           /\ owner' = "none" /\ doneB' = TRUE
           /\ UNCHANGED <<installations, observedA, observedB, doneA>>
UnsafeReadA == /\ Unsafe /\ ~observedA /\ installations = {}
               /\ observedA' = TRUE
               /\ UNCHANGED <<installations, owner, observedB, doneA, doneB>>
UnsafeReadB == /\ Unsafe /\ ~observedB /\ installations = {}
               /\ observedB' = TRUE
               /\ UNCHANGED <<installations, owner, observedA, doneA, doneB>>
UnsafeWriteA == /\ Unsafe /\ observedA /\ ~doneA
                /\ installations' = installations \cup {"A"} /\ doneA' = TRUE
                /\ UNCHANGED <<owner, observedA, observedB, doneB>>
UnsafeWriteB == /\ Unsafe /\ observedB /\ ~doneB
                /\ installations' = installations \cup {"B"} /\ doneB' = TRUE
                /\ UNCHANGED <<owner, observedA, observedB, doneA>>

Next == LockA \/ LockB \/ WriteA \/ WriteB \/ RejectA \/ RejectB
        \/ UnsafeReadA \/ UnsafeReadB \/ UnsafeWriteA \/ UnsafeWriteB
Spec == Init /\ [][Next]_vars
TypeOK == /\ installations \subseteq {"A","B"}
          /\ owner \in {"none","A","B"}
          /\ observedA \in BOOLEAN /\ observedB \in BOOLEAN
          /\ doneA \in BOOLEAN /\ doneB \in BOOLEAN
OneInstallation == Cardinality(installations) <= 1
=============================================================================
