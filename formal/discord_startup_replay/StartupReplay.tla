--------------------------- MODULE StartupReplay ---------------------------
EXTENDS Naturals, FiniteSets
CONSTANT AdmissionCheck, GraceCheck, ShortGrace
Workers == {1, 2}
VARIABLES live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected, retryExpired, recovered
vars == <<live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected, retryExpired, recovered>>

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
        /\ retryExpired = FALSE
        /\ recovered = FALSE

Restart(w) == /\ w \in Workers \ live
              /\ live' = live \cup {w}
              /\ connected' = connected \cup {w}
              /\ seen' = seen \ {w}
              /\ UNCHANGED <<ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected, retryExpired, recovered>>

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
             /\ UNCHANGED <<live, connected, turns, crashed, redelivered, stopped, graceElapsed, disconnected, retryExpired, recovered>>

Gateway(w) == /\ ~redelivered /\ w \in live
              /\ redelivered' = TRUE
              /\ Claim(w)
              /\ UNCHANGED <<live, connected, seen, turns, crashed, stopped, graceElapsed, disconnected, retryExpired, recovered>>

Start(w) == /\ w \in live /\ ledger = "pending" /\ owner = w /\ turns < 2
            /\ turns' = turns + 1
            /\ ledger' = "handled"
            /\ UNCHANGED <<live, connected, seen, owner, crashed, redelivered, stopped, graceElapsed, takeover, disconnected, retryExpired, recovered>>

Crash(w) == /\ ~crashed /\ w \in live /\ ledger = "pending" /\ owner = w
            /\ live' = live \ {w}
            /\ connected' = connected \ {w}
            /\ crashed' = TRUE
            /\ UNCHANGED <<seen, ledger, owner, turns, redelivered, stopped, graceElapsed, takeover, disconnected, retryExpired, recovered>>

\* A PostgreSQL backend dies while the worker and its provider turn remain live.
ConnectionLoss(w) == /\ ~disconnected /\ ~recovered /\ w \in connected /\ ledger = "pending" /\ owner = w
                     /\ connected' = connected \ {w}
                     /\ disconnected' = TRUE
                     /\ UNCHANGED <<live, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, retryExpired, recovered>>

\* A retry within B reacquires the lock and commits a fresh heartbeat.
Recover(w) == /\ disconnected /\ ~retryExpired /\ ~graceElapsed
              /\ w = owner /\ w \in live /\ w \notin connected
              /\ connected' = connected \cup {w}
              /\ disconnected' = FALSE
              /\ recovered' = TRUE
              /\ UNCHANGED <<live, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, retryExpired>>

RetryExpired == /\ disconnected /\ ~retryExpired
                /\ retryExpired' = TRUE
                /\ UNCHANGED <<live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, graceElapsed, takeover, disconnected, recovered>>

Stop(w) == /\ disconnected /\ retryExpired /\ ~stopped /\ w \in live /\ w \notin connected
           /\ live' = live \ {w}
           /\ stopped' = TRUE
           /\ UNCHANGED <<connected, seen, ledger, owner, turns, crashed, redelivered, graceElapsed, takeover, disconnected, retryExpired, recovered>>

GraceElapsed == /\ (stopped \/ (ShortGrace /\ disconnected)) /\ ~graceElapsed
                /\ graceElapsed' = TRUE
                /\ UNCHANGED <<live, connected, seen, ledger, owner, turns, crashed, redelivered, stopped, takeover, disconnected, retryExpired, recovered>>

Next == (\E w \in Workers : Restart(w) \/ Replay(w) \/ Gateway(w)
                                \/ Start(w) \/ Crash(w) \/ ConnectionLoss(w) \/ Recover(w) \/ Stop(w))
        \/ GraceElapsed \/ RetryExpired
Spec == Init /\ [][Next]_vars
        /\ (\A w \in Workers : WF_vars(Restart(w))
                                /\ WF_vars(Replay(w)) /\ WF_vars(Start(w)))
NoDuplicate == turns <= 1
NoTakeoverOfLiveWorker == ~takeover
RecoveredKeepsServing == ~(recovered /\ stopped)
NoTransientRecovery == ~recovered
EventuallyAnswered == <> (turns >= 1)
=============================================================================
