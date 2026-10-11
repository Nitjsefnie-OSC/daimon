-------------------------- MODULE DiscordTestConnections --------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANT Shared
Tasks == {"background", "foreground"}
Connections == {"shared", "background", "foreground"}

ConnectionOf(task) == IF Shared THEN "shared" ELSE task

VARIABLE active
vars == <<active>>

Init == active = [connection \in Connections |-> {}]

Acquire(task) ==
    /\ task \in Tasks
    /\ active' = [active EXCEPT ![ConnectionOf(task)] = @ \cup {task}]

Release(task) ==
    /\ task \in Tasks
    /\ active' = [active EXCEPT ![ConnectionOf(task)] = @ \ {task}]

Next == \E task \in Tasks: (Acquire(task) \/ Release(task))
Spec == Init /\ [][Next]_vars

TypeOK == active \in [Connections -> SUBSET Tasks]
AtMostOneOwner == \A connection \in Connections: Cardinality(active[connection]) <= 1

=============================================================================
