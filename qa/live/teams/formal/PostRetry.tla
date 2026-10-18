----------------------------- MODULE PostRetry -----------------------------
EXTENDS Naturals

VARIABLES phase, posts, found
vars == <<phase, posts, found>>

Init == phase = "first" /\ posts = 0 /\ found = FALSE
FirstPost == phase = "first" /\ posts' = 1 /\ phase' = "reconcile" /\ UNCHANGED found
Found == phase = "reconcile" /\ found' = TRUE /\ phase' = "done" /\ UNCHANGED posts
Absent == phase = "reconcile" /\ found' = FALSE /\ phase' = "retry" /\ UNCHANGED posts
Retry == phase = "retry" /\ posts' = posts + 1 /\ phase' = "done" /\ UNCHANGED found
Next == FirstPost \/ Found \/ Absent \/ Retry

Safe == posts <= 2 /\ (found => posts = 1)
Spec == Init /\ [][Next]_vars

=============================================================================
