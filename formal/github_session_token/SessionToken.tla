----------------------------- MODULE SessionToken -----------------------------
EXTENDS Naturals
CONSTANT Fault
VARIABLES version, grant, oldLive, newLive, vault, resource,
          newRow, oldDue, selected, crashed
vars == <<version, grant, oldLive, newLive, vault, resource,
          newRow, oldDue, selected, crashed>>

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

VersionBump == /\ grant = "same" /\ version = 1
               /\ version' = 2
               /\ UNCHANGED <<grant, oldLive, newLive, vault, resource,
                               newRow, oldDue, selected, crashed>>
Mint == /\ grant = "same" /\ version = 2 /\ newRow = "none"
        /\ newLive' = TRUE /\ newRow' = "stored"
        /\ UNCHANGED <<version, grant, oldLive, vault, resource,
                        oldDue, selected, crashed>>
SwapResource == /\ newRow = "stored" /\ resource = "old" /\ newLive
                /\ resource' = "new"
                /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                                newRow, oldDue, selected, crashed>>
SwapVault == /\ newRow = "stored" /\ vault = "old" /\ newLive
             /\ vault' = "new"
             /\ UNCHANGED <<version, grant, oldLive, newLive, resource,
                             newRow, oldDue, selected, crashed>>
Deliver == /\ newRow = "stored" /\ vault = "new" /\ resource = "new"
           /\ newRow' = "delivered"
           /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                           resource, oldDue, selected, crashed>>
RetireOld == /\ newRow = "delivered" /\ vault = "new" /\ resource = "new"
             /\ oldDue' = TRUE /\ newRow' = "retired"
             /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                             resource, selected, crashed>>
SelectSweep == /\ oldDue /\ oldLive /\ ~selected
               /\ selected' = TRUE
               /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                               resource, newRow, oldDue, crashed>>
RestoreOld == /\ selected /\ oldLive /\ grant = "same"
              /\ vault' = "old" /\ resource' = "old"
              /\ oldDue' = FALSE
              /\ UNCHANGED <<version, grant, oldLive, newLive,
                              newRow, selected, crashed>>
RecheckSweep == /\ selected /\ oldDue /\ vault = "new"
                /\ oldLive' = FALSE /\ selected' = FALSE
                /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                newRow, oldDue, crashed>>
DropSelection == /\ selected /\ ~oldDue
                 /\ selected' = FALSE
                 /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                                 resource, newRow, oldDue, crashed>>
Crash == /\ newRow = "stored" /\ ~crashed
         /\ crashed' = TRUE
         /\ UNCHANGED <<version, grant, oldLive, newLive, vault,
                         resource, newRow, oldDue, selected>>
RemoveGrant == /\ grant = "same"
               /\ grant' = "removed" /\ oldLive' = FALSE /\ newLive' = FALSE
               /\ UNCHANGED <<version, vault, resource, newRow,
                               oldDue, selected, crashed>>

UnsafeVersion == /\ Fault = "VersionRevoke" /\ version = 2 /\ vault = "old"
                 /\ oldLive /\ oldLive' = FALSE
                 /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed>>
UnsafePartial == /\ Fault = "PartialRevoke" /\ resource = "new"
                 /\ vault = "old" /\ oldLive /\ oldLive' = FALSE
                 /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed>>
UnsafePartialCleanup == /\ Fault = "PartialCleanup" /\ resource = "new"
                        /\ vault = "old" /\ newLive /\ newLive' = FALSE
                        /\ UNCHANGED <<version, grant, oldLive, vault, resource,
                                        newRow, oldDue, selected, crashed>>
UnsafeCrash == /\ Fault = "CrashCleanup" /\ crashed /\ vault = "new"
               /\ newRow = "stored" /\ newLive /\ newLive' = FALSE
               /\ UNCHANGED <<version, grant, oldLive, vault, resource,
                               newRow, oldDue, selected, crashed>>
UnsafeSweep == /\ Fault = "SweepRace" /\ selected /\ ~oldDue
               /\ vault = "old" /\ oldLive /\ oldLive' = FALSE
               /\ UNCHANGED <<version, grant, newLive, vault, resource,
                               newRow, oldDue, selected, crashed>>
UnsafeRemoved == /\ Fault = "RestoreRemoved" /\ grant = "removed"
                 /\ ~oldLive /\ oldLive' = TRUE
                 /\ UNCHANGED <<version, grant, newLive, vault, resource,
                                 newRow, oldDue, selected, crashed>>

Next == VersionBump \/ Mint \/ SwapResource \/ SwapVault \/ Deliver \/ RetireOld
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
LiveSessionHasToken == grant = "same" =>
                       (IF vault = "old" THEN oldLive ELSE newLive)
                       /\ (IF resource = "old" THEN oldLive ELSE newLive)
RemovedAccessInvalid == grant = "removed" => ~oldLive /\ ~newLive
=============================================================================
