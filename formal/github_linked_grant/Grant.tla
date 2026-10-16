-------------------------- MODULE Grant --------------------------
EXTENDS Naturals, TLC

CONSTANT Recheck
VARIABLES phase, manager, linked, covered, associated, checkedGeneration,
          generation, granted, validAtCommit

vars == <<phase, manager, linked, covered, associated,
          checkedGeneration, generation, granted, validAtCommit>>

Init == /\ phase = "start"
        /\ manager = TRUE /\ linked = TRUE /\ covered = TRUE
        /\ associated = TRUE /\ generation = 0
        /\ checkedGeneration = 2 /\ granted = FALSE
        /\ validAtCommit = FALSE

GitHubCheck == /\ phase = "start"
               /\ phase' = "checked"
               /\ checkedGeneration' = generation
               /\ UNCHANGED <<manager, linked, covered, associated,
                               generation, granted, validAtCommit>>

RevokeManager == /\ phase = "checked" /\ manager
                 /\ manager' = FALSE
                 /\ UNCHANGED <<phase, linked, covered, associated,
                                 checkedGeneration, generation, granted, validAtCommit>>

Unlink == /\ phase = "checked" /\ linked
          /\ linked' = FALSE /\ generation' = generation + 1
          /\ UNCHANGED <<phase, manager, covered, associated,
                          checkedGeneration, granted, validAtCommit>>

Suspend == /\ phase = "checked" /\ covered
           /\ covered' = FALSE
           /\ UNCHANGED <<phase, manager, linked, associated,
                           checkedGeneration, generation, granted, validAtCommit>>

Disassociate == /\ phase = "checked" /\ associated
                /\ associated' = FALSE
                /\ UNCHANGED <<phase, manager, linked, covered,
                                checkedGeneration, generation, granted, validAtCommit>>

Valid == manager /\ linked /\ covered /\ associated
         /\ checkedGeneration = generation

Commit == /\ phase = "checked"
          /\ phase' = "done"
          /\ granted' = IF Recheck THEN Valid ELSE TRUE
          /\ validAtCommit' = Valid
          /\ UNCHANGED <<manager, linked, covered, associated,
                          checkedGeneration, generation>>

Next == GitHubCheck \/ RevokeManager \/ Unlink \/ Suspend \/ Disassociate \/ Commit
Spec == Init /\ [][Next]_vars
Safe == granted => validAtCommit
==================================================================
