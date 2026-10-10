--------------------------- MODULE StartupReplay ---------------------------
EXTENDS Naturals, FiniteSets
CONSTANT AdmissionCheck
Workers == {1, 2}
VARIABLES live, seen, ledger, owner, turns, crashed, redelivered
vars == <<live, seen, ledger, owner, turns, crashed, redelivered>>

\* One addressed message arrives during downtime inside the configured window.
Init == /\ live = {}
        /\ seen = {}
        /\ ledger = "new"
        /\ owner = 0
        /\ turns = 0
        /\ crashed = FALSE
        /\ redelivered = FALSE

Restart(w) == /\ w \in Workers \ live
              /\ live' = live \cup {w}
              /\ seen' = seen \ {w}
              /\ UNCHANGED <<ledger, owner, turns, crashed, redelivered>>

Claim(w) == /\ w \in live
            /\ IF ~AdmissionCheck \/ ledger = "new"
                  \/ (ledger = "pending" /\ owner \notin live)
               THEN /\ ledger' = "pending" /\ owner' = w
               ELSE /\ UNCHANGED <<ledger, owner>>

Replay(w) == /\ w \in live /\ w \notin seen
             /\ seen' = seen \cup {w}
             /\ Claim(w)
             /\ UNCHANGED <<live, turns, crashed, redelivered>>

Gateway(w) == /\ ~redelivered /\ w \in live
              /\ redelivered' = TRUE
              /\ Claim(w)
              /\ UNCHANGED <<live, seen, turns, crashed>>

Start(w) == /\ w \in live /\ ledger = "pending" /\ owner = w /\ turns < 2
            /\ turns' = turns + 1
            /\ ledger' = "handled"
            /\ UNCHANGED <<live, seen, owner, crashed, redelivered>>

Crash(w) == /\ ~crashed /\ w \in live /\ ledger = "pending" /\ owner = w
            /\ live' = live \ {w}
            /\ crashed' = TRUE
            /\ UNCHANGED <<seen, ledger, owner, turns, redelivered>>

Next == (\E w \in Workers : Restart(w) \/ Replay(w) \/ Gateway(w)
                                \/ Start(w) \/ Crash(w))
Spec == Init /\ [][Next]_vars
        /\ (\A w \in Workers : WF_vars(Restart(w))
                                /\ WF_vars(Replay(w)) /\ WF_vars(Start(w)))
NoDuplicate == turns <= 1
EventuallyAnswered == <> (turns >= 1)
=============================================================================
