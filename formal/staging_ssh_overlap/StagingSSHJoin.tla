------------------------- MODULE StagingSSHJoin -------------------------
EXTENDS Naturals, TLC

CONSTANT UnsafeMissingJoin

KeyResult == {"not_started", "running", "success", "failure"}
Status == {"absent", "success", "failure", "incomplete"}
Join == {"pending", "passed", "failed"}
Workflow == {"active", "failed"}
Refresh == {"not_started", "started"}

VARIABLES key, status, joined, workflow, refresh, waitTicks
vars == <<key, status, joined, workflow, refresh, waitTicks>>

Init ==
    /\ key = "not_started"
    /\ status = "absent"
    /\ joined = "pending"
    /\ workflow = "active"
    /\ refresh = "not_started"
    /\ waitTicks = 0

Start ==
    /\ key = "not_started"
    /\ key' = "running"
    /\ UNCHANGED <<status, joined, workflow, refresh, waitTicks>>

FinishSuccess ==
    /\ key = "running"
    /\ status = "absent"
    /\ key' = "success"
    /\ status' = "success"
    /\ UNCHANGED <<joined, workflow, refresh, waitTicks>>

FinishFailure ==
    /\ key = "running"
    /\ status = "absent"
    /\ key' = "failure"
    /\ status' = "failure"
    /\ UNCHANGED <<joined, workflow, refresh, waitTicks>>

WriteIncompleteStatus ==
    /\ key = "running"
    /\ status = "absent"
    /\ status' = "incomplete"
    /\ UNCHANGED <<key, joined, workflow, refresh, waitTicks>>

WaitTick ==
    /\ status = "absent"
    /\ joined = "pending"
    /\ waitTicks < 2
    /\ waitTicks' = waitTicks + 1
    /\ UNCHANGED <<key, status, joined, workflow, refresh>>

JoinSuccess ==
    /\ status = "success"
    /\ joined = "pending"
    /\ joined' = "passed"
    /\ UNCHANGED <<key, status, workflow, refresh, waitTicks>>

JoinFailure ==
    /\ status = "failure"
    /\ joined = "pending"
    /\ joined' = "failed"
    /\ workflow' = "failed"
    /\ UNCHANGED <<key, status, refresh, waitTicks>>

JoinIncomplete ==
    /\ status = "incomplete"
    /\ joined = "pending"
    /\ joined' = "failed"
    /\ workflow' = "failed"
    /\ UNCHANGED <<key, status, refresh, waitTicks>>

WaitTimeout ==
    /\ status = "absent"
    /\ joined = "pending"
    /\ waitTicks = 2
    /\ joined' = "failed"
    /\ workflow' = "failed"
    /\ UNCHANGED <<key, status, refresh, waitTicks>>

RefreshAfterJoin ==
    /\ workflow = "active"
    /\ refresh = "not_started"
    /\ joined = "passed"
    /\ status = "success"
    /\ refresh' = "started"
    /\ UNCHANGED <<key, status, joined, workflow, waitTicks>>

UnsafeRefreshBeforeJoin ==
    /\ UnsafeMissingJoin
    /\ workflow = "active"
    /\ refresh = "not_started"
    /\ key = "running"
    /\ joined = "pending"
    /\ refresh' = "started"
    /\ UNCHANGED <<key, status, joined, workflow, waitTicks>>

Next == Start \/ FinishSuccess \/ FinishFailure \/ WriteIncompleteStatus
    \/ WaitTick \/ JoinSuccess \/ JoinFailure \/ JoinIncomplete \/ WaitTimeout
    \/ RefreshAfterJoin \/ UnsafeRefreshBeforeJoin \/ UNCHANGED vars

NoRefreshBeforeJoin == refresh = "not_started" \/ joined = "passed"
NoRefreshAfterFailedJoin == workflow = "active" \/ refresh = "not_started"

Spec == Init /\ [][Next]_vars
=============================================================================
