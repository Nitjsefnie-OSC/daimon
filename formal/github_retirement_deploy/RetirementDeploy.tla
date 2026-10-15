----------------------- MODULE RetirementDeploy -----------------------
EXTENDS TLC

CONSTANTS Phased, PreserveModes, UpsertModeOnGrant, BackfillAllRetained
VARIABLES schema, mcp, worker, turn, failed, mode, grantVisible, installation,
          newMode, newGrant
vars == <<schema, mcp, worker, turn, failed, mode, grantVisible, installation,
          newMode, newGrant>>

Init == /\ schema = "legacy"
        /\ mcp = "old"
        /\ worker = "old"
        /\ turn = "idle"
        /\ failed = FALSE
        /\ mode \in {"app", "legacy", "missing"}
        /\ grantVisible = TRUE
        /\ installation \in {"usable", "unusable"}
        /\ newMode = "missing"
        /\ newGrant = "absent"

StartTurn == /\ worker = "old" /\ turn = "idle"
             /\ turn' = "active"
             /\ UNCHANGED <<schema, mcp, worker, failed, mode, grantVisible, installation,
                           newMode, newGrant>>
EndTurn == /\ turn = "active"
           /\ turn' = "idle"
           /\ UNCHANGED <<schema, mcp, worker, failed, mode, grantVisible, installation,
                         newMode, newGrant>>
Expand == /\ Phased /\ schema = "legacy"
          /\ schema' = "expanded"
          /\ mode' = IF ~PreserveModes THEN "missing"
                     ELSE IF BackfillAllRetained \/ installation = "usable"
                          THEN "app" ELSE mode
          /\ UNCHANGED <<mcp, worker, turn, failed, grantVisible, installation,
                        newMode, newGrant>>
OneShot == /\ ~Phased /\ schema = "legacy"
           /\ schema' = "contracted"
           /\ mode' = "missing"
           /\ UNCHANGED <<mcp, worker, turn, failed, grantVisible, installation,
                         newMode, newGrant>>
DeployMcp == /\ schema \in {"expanded", "contracted"}
             /\ mcp = "old"
             /\ mcp' = "new"
             /\ UNCHANGED <<schema, worker, turn, failed, mode, grantVisible, installation,
                           newMode, newGrant>>
DeployWorker == /\ schema \in {"expanded", "contracted"}
                /\ worker = "old"
                /\ worker' = "new"
                /\ UNCHANGED <<schema, mcp, turn, failed, mode, grantVisible, installation,
                              newMode, newGrant>>
Contract == /\ Phased /\ schema = "expanded"
            /\ mcp = "new" /\ worker = "new" /\ turn = "idle"
            /\ schema' = "contracted"
            /\ mode' = "missing"
            /\ newMode' = "missing"
            /\ UNCHANGED <<mcp, worker, turn, failed, grantVisible, installation, newGrant>>
InstallationRecovers == /\ schema = "expanded" /\ installation = "unusable"
                        /\ installation' = "usable"
                        /\ UNCHANGED <<schema, mcp, worker, turn, failed, mode, grantVisible,
                                      newMode, newGrant>>
OldGrantWrite == /\ schema = "expanded" /\ installation = "usable"
                 /\ (worker = "old" \/ turn = "active")
                 /\ grantVisible' = (mode = "app")
                 /\ UNCHANGED <<schema, mcp, worker, turn, failed, mode, installation,
                               newMode, newGrant>>
NewGrant == /\ schema = "expanded" /\ mcp = "new" /\ newGrant = "absent"
            /\ newGrant' = "visible"
            /\ newMode' = IF UpsertModeOnGrant THEN "app" ELSE "missing"
            /\ UNCHANGED <<schema, mcp, worker, turn, failed, mode, grantVisible,
                          installation>>
OldNewGrantWrite == /\ schema = "expanded" /\ newGrant = "visible"
                    /\ (worker = "old" \/ turn = "active")
                    /\ newGrant' = IF newMode = "app" THEN "visible" ELSE "hidden"
                    /\ UNCHANGED <<schema, mcp, worker, turn, failed, mode, grantVisible,
                                  installation, newMode>>
OldSchemaRead == /\ schema = "contracted"
                 /\ (mcp = "old" \/ worker = "old" \/ turn = "active")
                 /\ failed' = TRUE
                 /\ UNCHANGED <<schema, mcp, worker, turn, mode, grantVisible, installation,
                               newMode, newGrant>>

Next == StartTurn \/ EndTurn \/ Expand \/ OneShot \/ DeployMcp \/ DeployWorker
        \/ Contract \/ InstallationRecovers \/ OldGrantWrite \/ NewGrant
        \/ OldNewGrantWrite \/ OldSchemaRead
Spec == Init /\ [][Next]_vars
TypeOK == /\ schema \in {"legacy", "expanded", "contracted"}
          /\ mcp \in {"old", "new"}
          /\ worker \in {"old", "new"}
          /\ turn \in {"idle", "active"}
          /\ failed \in BOOLEAN
          /\ mode \in {"app", "legacy", "missing"}
          /\ grantVisible \in BOOLEAN
          /\ installation \in {"usable", "unusable"}
          /\ newMode \in {"app", "missing"}
          /\ newGrant \in {"absent", "visible", "hidden"}
NoOldSchemaReadFailure == ~failed
NoLiveGrantHidden == grantVisible /\ newGrant # "hidden"
=======================================================================
