--------------------------- MODULE StartupReplay ---------------------------
EXTENDS Naturals, FiniteSets
CONSTANT AdmissionCheck, GraceCheck
Workers == {1, 2}
VARIABLES live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected
vars == <<live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected>>

\* One addressed message arrives during downtime inside the configured window.
Init == /\ live = {}
        /\ connected = {}
        /\ seen = {}
        /\ ledger = "new"
        /\ owner = 0
        /\ turns = 0
        /\ crashed = FALSE
        /\ redelivered = FALSE
        /\ stopped = FALSE
        /\ graceElapsed = FALSE
        /\ takeover = FALSE
        /\ disconnected = FALSE

Restart(w) == /\ w \in Workers \ live
              /\ live' = live \cup {w}
              /\ connected' = connected \cup {w}
              /\ seen' = seen \ {w}
              /\ UNCHANGED <<ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected>>

Claim(w) == /\ w \in live
            /\ IF ~AdmissionCheck \/ ledger = "new"
                  \/ (ledger = "pending" /\ owner \notin connected
                      /\ (~GraceCheck \/ owner \notin live \/ graceElapsed))
               THEN /\ ledger' = "pending" /\ owner' = w
                    /\ takeover' = (takeover \/ (owner # 0 /\ owner # w /\ owner \in live))
               ELSE /\ UNCHANGED <<ledger, owner, takeover>>

Replay(w) == /\ w \in live /\ w \notin seen
             /\ seen' = seen \cup {w}
             /\ Claim(w)
             /\ UNCHANGED <<live, connected, turns, crashed, redelivered, stopped, graceElapsed, disconnected>>

Gateway(w) == /\ ~redelivered /\ w \in live
              /\ redelivered' = TRUE
              /\ Claim(w)
              /\ UNCHANGED <<live, connected, seen, turns, crashed, stopped, graceElapsed, disconnected>>

Start(w) == /\ w \in live /\ ledger = "pending" /\ owner = w /\ turns < 2
            /\ turns' = turns + 1
            /\ ledger' = "handled"
            /\ UNCHANGED <<live, connected, seen, owner, crashed, redelivered, stopped, graceElapsed, takeover, disconnected>>

Crash(w) == /\ ~crashed /\ w \in live /\ ledger = "pending" /\ owner = w
            /\ live' = live \ {w}
            /\ connected' = connected \ {w}
            /\ crashed' = TRUE
            /\ UNCHANGED <<seen, ledger, owner, turns, redelivered, stopped, graceElapsed, takeover, disconnected>>

\* A PostgreSQL backend dies while the worker and its provider turn remain live.
ConnectionLoss(w) == /\ ~disconnected /\ w \in connected /\ ledger = "pending" /\ owner = w
                     /\ connected' = connected \ {w}
                     /\ disconnected' = TRUE
                     /\ UNCHANGED <<live, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover>>

Stop(w) == /\ disconnected /\ ~stopped /\ w \in live /\ w \notin connected
           /\ live' = live \ {w}
           /\ stopped' = TRUE
           /\ UNCHANGED <<connected, seen, ledger, owner, turns, crashed, redelivered, graceElapsed, takeover, disconnected>>

GraceElapsed == /\ stopped /\ ~graceElapsed
                /\ graceElapsed' = TRUE
                /\ UNCHANGED <<live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, takeover, disconnected>>

Next == (\E w \in Workers : Restart(w) \/ Replay(w) \/ Gateway(w)
                                \/ Start(w) \/ Crash(w) \/ ConnectionLoss(w) \/ Stop(w))
        \/ GraceElapsed
Spec == Init /\ [][Next]_vars
        /\ (\A w \in Workers : WF_vars(Restart(w))
                                /\ WF_vars(Replay(w)) /\ WF_vars(Start(w)))
NoDuplicate == turns <= 1
NoTakeoverOfLiveWorker == ~takeover
EventuallyAnswered == <> (turns >= 1)
=============================================================================
