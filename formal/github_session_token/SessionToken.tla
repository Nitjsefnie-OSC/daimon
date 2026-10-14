----------------------------- MODULE SessionToken -----------------------------
EXTENDS Naturals
CONSTANT Fault
VARIABLES version, grant, oldLive, newLive, vault, resource,
          newRow, oldDue, selected, crashed, lockPhase
vars == <<version, grant, oldLive, newLive, vault, resource,
          newRow, oldDue, selected, crashed, lockPhase>>

Init == /\ version = 1
        /\ grant = "same"
        /\ oldLive = TRUE
        /\ newLive = FALSE
        /\ vault = "old"
        /\ resource = "old"
        /\ newRow = "none"
        /\ oldDue = FALSE
        /\ selected = FALSE
        /\ crashed = FALSE
        /\ lockPhase = "free"

VersionBump == /\ grant = "same" /\ version = 1 /\ lockPhase = "free"
               /\ version' = 2
               /\ UNCHANGED <<grant, oldLive, newLive, vault, resource,
                               newRow, oldDue, selected, crashed, lockPhase>>
Mint == /\ grant = "same" /\ version = 2 /\ newRow = "none" /\ lockPhase = "free"
        /\ newLive' = TRUE /\ newRow' = "stored"
        /\ UNCHANGED <<version, grant, oldLive, vault, resource,
                        oldDue, selected, crashed, lockPhase>>
SwapResource == /\ newRow = "stored" /\ resource = "old" /\ newLive /\ lockPhase = "free"
                /\ resource' = "new"
                /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                                newRow, oldDue, selected, crashed, lockPhase>>
SwapVault == /\ newRow = "stored" /\ vault = "old" /\ newLive /\ lockPhase = "free"
             /\ vault' = "new"
             /\ UNCHANGED <<version, grant, oldLive, newLive, resource,
                             newRow, oldDue, selected, crashed, lockPhase>>
LockDelivery == /\ lockPhase = "free" /\ newRow = "stored"
                /\ vault = "new" /\ resource = "new"
                /\ lockPhase' = "delivery"
                /\ UNCHANGED <<version, grant, oldLive, newLive, vault, resource,
                                newRow, oldDue, selected, crashed>>
Deliver == /\ lockPhase = "delivery" /\ grant = "same"
           /\ newRow = "stored" /\ vault = "new" /\ resource = "new"
           /\ newRow' = "delivered" /\ lockPhase' = "free"
           /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                           resource, oldDue, selected, crashed>>
LockRetire == /\ lockPhase = "free" /\ newRow = "delivered"
              /\ lockPhase' = "retire"
              /\ UNCHANGED <<version, grant, oldLive, newLive, vault, resource,
                              newRow, oldDue, selected, crashed>>
RetireOld == /\ lockPhase = "retire" /\ grant = "same"
             /\ newRow = "delivered" /\ vault = "new" /\ resource = "new"
             /\ oldDue' = TRUE /\ newRow' = "retired" /\ lockPhase' = "free"
             /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                             resource, selected, crashed>>
AbortDecision == /\ lockPhase # "free" /\ lockPhase' = "free"
                 /\ UNCHANGED <<version, grant, oldLive, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed>>
SelectSweep == /\ oldDue /\ oldLive /\ ~selected
               /\ selected' = TRUE
               /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                               resource, newRow, oldDue, crashed, lockPhase>>
RestoreOld == /\ selected /\ oldLive /\ grant = "same"
              /\ vault' = "old" /\ resource' = "old"
              /\ oldDue' = FALSE
              /\ UNCHANGED <<version, grant, oldLive, newLive,
                              newRow, selected, crashed, lockPhase>>
RecheckSweep == /\ selected /\ oldDue /\ vault = "new"
                /\ oldLive' = FALSE /\ selected' = FALSE
                /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                newRow, oldDue, crashed, lockPhase>>
DropSelection == /\ selected /\ ~oldDue
                 /\ selected' = FALSE
                 /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                                 resource, newRow, oldDue, crashed, lockPhase>>
Crash == /\ newRow = "stored" /\ ~crashed
         /\ crashed' = TRUE
         /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                         resource, newRow, oldDue, selected, lockPhase>>
RemoveGrant == /\ grant = "same" /\ lockPhase = "free"
               /\ grant' = "removed" /\ oldLive' = FALSE /\ newLive' = FALSE
               /\ UNCHANGED <<version, vault, resource, newRow,
                               oldDue, selected, crashed, lockPhase>>

UnsafeVersion == /\ Fault = "VersionRevoke" /\ version = 2 /\ vault = "old"
                 /\ oldLive /\ oldLive' = FALSE
                 /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed, lockPhase>>
UnsafePartial == /\ Fault = "PartialRevoke" /\ resource = "new"
                 /\ vault = "old" /\ oldLive /\ oldLive' = FALSE
                 /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed, lockPhase>>
UnsafePartialCleanup == /\ Fault = "PartialCleanup" /\ resource = "new"
                        /\ vault = "old" /\ newLive /\ newLive' = FALSE
                        /\ UNCHANGED <<version, grant, oldLive, vault, resource,
                                        newRow, oldDue, selected, crashed, lockPhase>>
UnsafeCrash == /\ Fault = "CrashCleanup" /\ crashed /\ vault = "new"
               /\ newRow = "stored" /\ newLive /\ newLive' = FALSE
               /\ UNCHANGED <<version, grant, oldLive, vault, resource,
                               newRow, oldDue, selected, crashed, lockPhase>>
UnsafeSweep == /\ Fault = "SweepRace" /\ selected /\ ~oldDue
               /\ vault = "old" /\ oldLive /\ oldLive' = FALSE
               /\ UNCHANGED <<version, grant, newLive, vault, resource,
                               newRow, oldDue, selected, crashed, lockPhase>>
UnsafeRemoved == /\ Fault = "RestoreRemoved" /\ grant = "removed"
                 /\ ~oldLive /\ oldLive' = TRUE
                 /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed, lockPhase>>

UnsafeLongLock == /\ Fault = "LongLock" /\ lockPhase = "free"
                  /\ version = 2 /\ newRow = "none"
                  /\ lockPhase' = "network"
                  /\ UNCHANGED <<version, grant, oldLive, newLive, vault, resource,
                                  newRow, oldDue, selected, crashed>>
Next == VersionBump \/ Mint \/ SwapResource \/ SwapVault \/ LockDelivery \/ Deliver
        \/ LockRetire \/ RetireOld \/ AbortDecision \/ UnsafeLongLock
        \/ SelectSweep \/ RestoreOld \/ RecheckSweep \/ DropSelection
        \/ Crash \/ RemoveGrant \/ UnsafeVersion \/ UnsafePartial
        \/ UnsafePartialCleanup
        \/ UnsafeCrash \/ UnsafeSweep \/ UnsafeRemoved
Spec == Init /\ [][Next]_vars
TypeOK == /\ version \in {1,2} /\ grant \in {"same","removed"}
          /\ oldLive \in BOOLEAN /\ newLive \in BOOLEAN
          /\ vault \in {"old","new"} /\ resource \in {"old","new"}
          /\ newRow \in {"none","stored","delivered","retired"}
          /\ oldDue \in BOOLEAN /\ selected \in BOOLEAN /\ crashed \in BOOLEAN
          /\ lockPhase \in {"free", "delivery", "retire", "network"}
LiveSessionHasToken == grant = "same" =>
                       (IF vault = "old" THEN oldLive ELSE newLive)
                       /\ (IF resource = "old" THEN oldLive ELSE newLive)
RemovedAccessInvalid == grant = "removed" => ~oldLive /\ ~newLive
NoNetworkUnderLock == lockPhase # "network"
=============================================================================
