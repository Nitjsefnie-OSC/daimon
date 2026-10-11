---------------------------- MODULE StagingTag ----------------------------
EXTENDS FiniteSets, Naturals, TLC

CONSTANTS SameDigest, UnsafeOverwrite
Runners == {"first", "rerun"}
Digests == {"A", "B"}
Expected(r) == IF r = "first" \/ SameDigest THEN "A" ELSE "B"

VARIABLES tag, history, phase
vars == <<tag, history, phase>>

Init == /\ tag \in {"none"} \cup Digests
        /\ history = IF tag = "none" THEN {} ELSE {tag}
        /\ phase = [r \in Runners |-> "start"]

Lookup(r) ==
    /\ phase[r] = "start"
    /\ phase' = [phase EXCEPT ![r] =
         IF tag = "none" THEN "missing"
         ELSE IF tag = Expected(r) THEN "done" ELSE "rejected"]
    /\ UNCHANGED <<tag, history>>

Create(r) ==
    /\ phase[r] = "missing"
    /\ tag = "none"
    /\ tag' = Expected(r)
    /\ history' = history \cup {Expected(r)}
    /\ phase' = [phase EXCEPT ![r] = "verify"]

ResolveCreateRace(r) ==
    /\ phase[r] = "missing"
    /\ tag # "none"
    /\ IF UnsafeOverwrite
          THEN /\ tag' = Expected(r)
               /\ history' = history \cup {Expected(r)}
               /\ phase' = [phase EXCEPT ![r] = "verify"]
          ELSE /\ phase' = [phase EXCEPT ![r] =
                        IF tag = Expected(r) THEN "done" ELSE "rejected"]
               /\ UNCHANGED <<tag, history>>

Verify(r) ==
    /\ phase[r] = "verify"
    /\ phase' = [phase EXCEPT ![r] =
         IF tag = Expected(r) THEN "done" ELSE "rejected"]
    /\ UNCHANGED <<tag, history>>

Next == \E r \in Runners:
    Lookup(r) \/ Create(r) \/ ResolveCreateRace(r) \/ Verify(r)
Spec == Init /\ [][Next]_vars

TypeOK == /\ tag \in {"none"} \cup Digests
          /\ history \subseteq Digests
          /\ phase \in [Runners -> {"start", "missing", "verify", "done", "rejected"}]
ImmutableTag == Cardinality(history) <= 1
AcceptedDigestMatches == \A r \in Runners:
    phase[r] = "done" => tag = Expected(r)

=============================================================================
