------------------------- MODULE BudgetChannelRecovery -------------------------
EXTENDS TLC

\* Old MA session is stamped A. A legacy DB row stores "unknown" instead.
\* Configs select the admitted channel and deliberately broken decisions.
CONSTANTS AdmittedChannel, Legacy, ReadSucceeds,
          SafeLegacy, SafeReplacement, SafeCheckpoint
ASSUME AdmittedChannel \in {"A", "B"}
ASSUME Legacy \in BOOLEAN /\ ReadSucceeds \in BOOLEAN
ASSUME SafeLegacy \in BOOLEAN /\ SafeReplacement \in BOOLEAN
ASSUME SafeCheckpoint \in BOOLEAN

Channels == {"A", "B"}
None == "none"
Unknown == "unknown"
Phases == {"created", "resolved", "prepared", "checkpointed", "missed", "billed"}

VARIABLES phase, stamp, stored, replaced, liveCheckpoint, recoveredCheckpoint,
          turnDebit
vars == <<phase, stamp, stored, replaced, liveCheckpoint, recoveredCheckpoint,
          turnDebit>>

Init ==
  /\ phase = "created"
  /\ stamp = "A"
  /\ stored = IF Legacy THEN Unknown ELSE "A"
  /\ replaced = FALSE
  /\ liveCheckpoint = None
  /\ recoveredCheckpoint = None
  /\ turnDebit = None

ResolveLegacy ==
  /\ phase = "created"
  /\ phase' = "resolved"
  /\ stored' = IF Legacy /\ ReadSucceeds /\ SafeLegacy THEN stamp ELSE stored
  /\ UNCHANGED <<stamp, replaced, liveCheckpoint, recoveredCheckpoint, turnDebit>>

Prepare ==
  /\ phase = "resolved"
  /\ phase' = "prepared"
  /\ replaced' = IF stored = Unknown
                  THEN ~SafeLegacy
                  ELSE IF stored # AdmittedChannel THEN SafeReplacement ELSE FALSE
  /\ stamp' = IF replaced' THEN AdmittedChannel ELSE stamp
  /\ UNCHANGED <<stored, liveCheckpoint, recoveredCheckpoint, turnDebit>>

Checkpoint ==
  /\ phase = "prepared"
  /\ phase' = "checkpointed"
  /\ liveCheckpoint' = IF replaced THEN
                         (IF SafeCheckpoint THEN "A" ELSE AdmittedChannel)
                       ELSE None
  /\ UNCHANGED <<stamp, stored, replaced, recoveredCheckpoint, turnDebit>>

MissLiveEvent ==
  /\ phase = "checkpointed"
  /\ phase' = "missed"
  /\ UNCHANGED <<stamp, stored, replaced, liveCheckpoint,
                 recoveredCheckpoint, turnDebit>>

Sweep ==
  /\ phase = "missed"
  /\ phase' = "billed"
  /\ recoveredCheckpoint' = IF replaced THEN "A" ELSE None
  /\ turnDebit' = stamp
  /\ UNCHANGED <<stamp, stored, replaced, liveCheckpoint>>

Next == ResolveLegacy \/ Prepare \/ Checkpoint \/ MissLiveEvent \/ Sweep
Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ phase \in Phases
  /\ stamp \in Channels
  /\ stored \in Channels \cup {Unknown}
  /\ replaced \in BOOLEAN
  /\ liveCheckpoint \in Channels \cup {None}
  /\ recoveredCheckpoint \in Channels \cup {None}
  /\ turnDebit \in Channels \cup {None}

NoSpuriousReplacement ==
  (phase \in {"prepared", "checkpointed", "missed", "billed"}
   /\ AdmittedChannel = "A") => ~replaced
CorrectDebit ==
  (turnDebit # None /\ (stored # Unknown \/ ReadSucceeds))
  => turnDebit = AdmittedChannel
CheckpointDebit ==
  recoveredCheckpoint # None => liveCheckpoint = recoveredCheckpoint
=============================================================================
