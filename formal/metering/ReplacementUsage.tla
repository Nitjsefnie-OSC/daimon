-------------------------- MODULE ReplacementUsage ---------------------------
EXTENDS Naturals, TLC

\* One replacing turn: the checkpoint executes the predecessor on channel A;
\* the successor executes on its admission channel B. Outcome prices are
\* diagnostic, while recorder and sweep rows are the money authority.
CONSTANT RegisterCheckpointModel
ASSUME RegisterCheckpointModel \in BOOLEAN

Calls == {"checkpoint", "successor"}
None == "none"
Models == {"old", "new"}
Channels == {"A", "B"}
Phases == {"start", "checkpoint_bound", "checkpoint_called", "successor_bound",
           "successor_called", "swept"}

VARIABLES phase, observedModel, liveDebit, sweepDebit
vars == <<phase, observedModel, liveDebit, sweepDebit>>

Init ==
  /\ phase = "start"
  /\ observedModel = [c \in Calls |-> None]
  /\ liveDebit = [c \in Calls |-> None]
  /\ sweepDebit = [c \in Calls |-> None]

BindCheckpoint ==
  /\ phase = "start"
  /\ phase' = "checkpoint_bound"
  /\ UNCHANGED <<observedModel, liveDebit, sweepDebit>>

CheckpointCall ==
  /\ phase = "checkpoint_bound"
  /\ phase' = "checkpoint_called"
  /\ observedModel' = [observedModel EXCEPT
       !["checkpoint"] = IF RegisterCheckpointModel THEN "old" ELSE None]
  /\ liveDebit' = [liveDebit EXCEPT !["checkpoint"] = "A"]
  /\ UNCHANGED sweepDebit

BindSuccessor ==
  /\ phase = "checkpoint_called"
  /\ phase' = "successor_bound"
  /\ UNCHANGED <<observedModel, liveDebit, sweepDebit>>

SuccessorCall ==
  /\ phase = "successor_bound"
  /\ phase' = "successor_called"
  /\ observedModel' = [observedModel EXCEPT !["successor"] = "new"]
  /\ liveDebit' = [liveDebit EXCEPT !["successor"] = "B"]
  /\ UNCHANGED sweepDebit

Sweep ==
  /\ phase = "successor_called"
  /\ phase' = "swept"
  /\ sweepDebit' = [c \in Calls |-> IF liveDebit[c] = None
                     THEN IF c = "checkpoint" THEN "A" ELSE "B" ELSE None]
  /\ UNCHANGED <<observedModel, liveDebit>>

Next == BindCheckpoint \/ CheckpointCall \/ BindSuccessor \/ SuccessorCall \/ Sweep
Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ phase \in Phases
  /\ observedModel \in [Calls -> Models \cup {None}]
  /\ liveDebit \in [Calls -> Channels \cup {None}]
  /\ sweepDebit \in [Calls -> Channels \cup {None}]
PricedObservation ==
  (phase \in {"checkpoint_called", "successor_bound", "successor_called", "swept"})
    => observedModel["checkpoint"] = "old"
CorrectChannels ==
  /\ liveDebit["checkpoint"] \in {None, "A"}
  /\ liveDebit["successor"] \in {None, "B"}
  /\ sweepDebit["checkpoint"] \in {None, "A"}
  /\ sweepDebit["successor"] \in {None, "B"}
NoDoubleDebit == \A c \in Calls : liveDebit[c] = None \/ sweepDebit[c] = None
=============================================================================
