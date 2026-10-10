------------------------- MODULE BudgetChannelRecovery -------------------------
EXTENDS TLC

\* One existing session stamped A, followed by a turn admitted under B.
\* SafeReplacement models the channel comparison in session_preparation.
CONSTANT SafeReplacement
ASSUME SafeReplacement \in BOOLEAN

Channels == {"A", "B"}
None == "none"
Phases == {"created", "changed", "ready", "missed", "billed"}

VARIABLES phase, stamp, admitted, charged
vars == <<phase, stamp, admitted, charged>>

Init ==
  /\ phase = "created"
  /\ stamp = "A"
  /\ admitted = None
  /\ charged = None

ChangePolicy ==
  /\ phase = "created"
  /\ phase' = "changed"
  /\ admitted' = "B"
  /\ UNCHANGED <<stamp, charged>>

Prepare ==
  /\ phase = "changed"
  /\ phase' = "ready"
  /\ stamp' = IF SafeReplacement THEN admitted ELSE stamp
  /\ UNCHANGED <<admitted, charged>>

MissLiveEvent ==
  /\ phase = "ready"
  /\ phase' = "missed"
  /\ UNCHANGED <<stamp, admitted, charged>>

Sweep ==
  /\ phase = "missed"
  /\ phase' = "billed"
  /\ charged' = stamp
  /\ UNCHANGED <<stamp, admitted>>

Next == ChangePolicy \/ Prepare \/ MissLiveEvent \/ Sweep
Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ phase \in Phases
  /\ stamp \in Channels
  /\ admitted \in Channels \cup {None}
  /\ charged \in Channels \cup {None}

CorrectDebit == charged # None => charged = admitted
=============================================================================
