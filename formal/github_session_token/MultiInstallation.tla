-------------------------- MODULE MultiInstallation --------------------------
EXTENDS Naturals, FiniteSets
CONSTANT Fault
Installations == {"A", "B"}
Repos == {"a", "b", "c"}
Token(i, n) == <<i, n>>
TokenIds == Installations \X {1, 2}
Chunks == {"0", "1", "2"}
ChunkSlots == {"A0", "A1", "B0", "B1"}
NewFaults == {"ChunkSafe", "AnyRowDetection", "AgeSafe", "ResetAgeOnContraction",
              "VaultSafe", "CreateBeforeDelete", "SnapshotContraction",
              "SweptContractionMissed", "ModeTransitionSafe",
              "TransitionKeepsLabels"}
VARIABLES home, grants, scope, live, expired, generation, fingerprint,
          invalidated, phase, readRepo, readHome, readInstallation,
          readScope, readGeneration, requested, returned, returnedUsable,
          lastAction, bBefore, bExpiredBefore, aBefore,
          workingInstallation, aliasInstallation, chunkPhase, mintedChunks,
          inventoryNames, vaultNames,
          chunkLive, chunkDesired, chunkAge, chunkAgeBefore, detectedChunks,
          ageDue, chunkAction, deliveryPhase, vaultCredentials,
          singleRowsUnlabelled
vars == <<home, grants, scope, live, expired, generation, fingerprint,
          invalidated, phase, readRepo, readHome, readInstallation,
          readScope, readGeneration, requested, returned, returnedUsable,
          lastAction, bBefore, bExpiredBefore, aBefore,
          workingInstallation, aliasInstallation, chunkPhase, mintedChunks,
          inventoryNames, vaultNames, chunkLive, chunkDesired, chunkAge,
          chunkAgeBefore, detectedChunks, ageDue, chunkAction,
          deliveryPhase, vaultCredentials, singleRowsUnlabelled>>
oldvars == <<home, grants, scope, live, expired, generation, fingerprint,
             invalidated, phase, readRepo, readHome, readInstallation,
             readScope, readGeneration, requested, returned, returnedUsable,
             lastAction, bBefore, bExpiredBefore, aBefore,
             workingInstallation, aliasInstallation, chunkPhase, mintedChunks,
             inventoryNames, vaultNames>>
chunkvars == <<chunkLive, chunkDesired, chunkAge, chunkAgeBefore,
               detectedChunks, ageDue, chunkAction, deliveryPhase,
               vaultCredentials, singleRowsUnlabelled>>

\* Two installations and three repos scale down groups and exact repository IDs
\* at github_app_session.py:401-452 and github_issued_tokens.py:90-136.
Init == /\ home = [a |-> "A", b |-> "B", c |-> "B"]
        /\ grants = [A |-> {"a"}, B |-> {"b"}]
        /\ scope = grants
        /\ live = [A |-> TRUE, B |-> Fault # "SingleToken"]
        /\ expired = [A |-> FALSE, B |-> FALSE]
        /\ generation = [A |-> 1, B |-> 1]
        /\ fingerprint = grants
        /\ invalidated = {}
        /\ phase = "idle" /\ readRepo = "none" /\ readHome = "none"
        /\ readInstallation = "none" /\ readScope = {}
        /\ readGeneration = 0 /\ requested = "none" /\ returned = "none"
        /\ returnedUsable = FALSE /\ lastAction = "init"
        /\ bBefore = TRUE /\ bExpiredBefore = FALSE /\ aBefore = TRUE
        /\ workingInstallation = "A" /\ aliasInstallation = "A"
        /\ chunkPhase = "ready" /\ mintedChunks = {}
        /\ inventoryNames = [n \in Chunks |-> "none"]
        /\ vaultNames = [n \in Chunks |-> "none"]
        /\ chunkLive = IF Fault \in {"AgeSafe", "ResetAgeOnContraction"}
                       THEN ChunkSlots ELSE {"A0", "A1", "B0"}
        /\ chunkDesired = chunkLive
        /\ chunkAge = [c \in ChunkSlots |-> 39]
        /\ chunkAgeBefore = [c \in ChunkSlots |-> 39]
        /\ detectedChunks = {}
        /\ ageDue = FALSE /\ chunkAction = "init"
        /\ deliveryPhase = "delete"
        /\ vaultCredentials = {"1", "2", "3", "4", "5", "6", "7",
                               "8", "9", "10", "11", "12",
                               "alias", "copilot", "other"}
        /\ singleRowsUnlabelled = FALSE

Eligible(i, r) == live[i] /\ ~expired[i] /\ r \in scope[i] /\ r \in grants[i]
\* Proposed helper read of the installation mapping and vault token. Today's
\* github_app_session.py:504-513,629-632 delivers a single GH_TOKEN.
Pick(r) == IF Fault = "Fallback" /\ r = "c" /\ live["A"] THEN "A"
           ELSE IF Fault = "ExpiredLookup" /\ r = "a" /\ expired["A"] THEN "A"
           ELSE IF Eligible(home[r], r) THEN home[r] ELSE "none"
Read(r) == /\ phase = "idle" /\ r \in Repos
           /\ phase' = "pending" /\ readRepo' = r /\ readHome' = home[r]
           /\ readInstallation' = Pick(r)
           /\ readScope' = IF Pick(r) = "none" THEN {} ELSE scope[Pick(r)]
           /\ readGeneration' = IF Pick(r) = "none" THEN 0 ELSE generation[Pick(r)]
           /\ requested' = "none" /\ returned' = "none"
           /\ returnedUsable' = FALSE /\ lastAction' = "read"
           /\ UNCHANGED <<home, grants, scope, live, expired, generation,
                           fingerprint, invalidated, bBefore, bExpiredBefore, aBefore,
                           workingInstallation, aliasInstallation, chunkPhase,
                           mintedChunks, inventoryNames, vaultNames>>
\* The read and output can be interleaved with grant revocation. A cached token
\* may be output; invalidated tracks the assumption that GitHub rejects it.
Return == /\ phase = "pending"
          /\ phase' = "idle" /\ requested' = readRepo
          /\ returned' = readInstallation
          /\ returnedUsable' = (readInstallation # "none"
                                 /\ Token(readInstallation, readGeneration) \notin invalidated)
          /\ lastAction' = "return"
          /\ UNCHANGED <<home, grants, scope, live, expired, generation,
                          fingerprint, invalidated, readRepo, readHome,
                          readInstallation, readScope, readGeneration,
                          bBefore, bExpiredBefore, aBefore,
                          workingInstallation, aliasInstallation, chunkPhase,
                          mintedChunks, inventoryNames, vaultNames>>
\* Proposed per-installation hard-change invalidation. Grant removal is at
\* github_access.py:495-500; token-row recheck is github_issued_tokens.py:550-566.
RevokeA == /\ grants["A"] # {}
           /\ grants' = [grants EXCEPT !["A"] = {}]
           /\ scope' = [scope EXCEPT !["A"] = {}]
           /\ live' = IF Fault = "RevokeAll" THEN [A |-> FALSE, B |-> FALSE]
                       ELSE [live EXCEPT !["A"] = FALSE]
           /\ invalidated' = IF Fault = "NoUpstreamRevoke" THEN invalidated
                              ELSE invalidated \cup {Token("A", generation["A"])}
           /\ aliasInstallation' = IF workingInstallation = "A" THEN "none"
                                    ELSE aliasInstallation
           /\ fingerprint' = IF Fault = "OldFingerprint" THEN fingerprint ELSE grants'
           /\ lastAction' = "revokeA" /\ bBefore' = live["B"]
           /\ UNCHANGED <<home, expired, generation, phase, readRepo, readHome,
                           readInstallation, readScope, readGeneration,
                           requested, returned, returnedUsable, bExpiredBefore, aBefore,
                           workingInstallation, chunkPhase, mintedChunks,
                           inventoryNames, vaultNames>>
\* A URL can remain the same while its installation changes. Current snapshot
\* stores only URLs: session_snapshot.py:143-145, session_compat.py:173-177.
ReassignA == /\ home["a"] = "A" /\ grants["A"] = {"a"}
             /\ home' = [home EXCEPT !["a"] = "B"]
             /\ grants' = [grants EXCEPT !["A"] = {}, !["B"] = @ \cup {"a"}]
             /\ scope' = [A |-> {}, B |-> {}]
             /\ live' = [A |-> FALSE, B |-> FALSE]
             /\ invalidated' = invalidated \cup
                                 {Token("A", generation["A"]), Token("B", generation["B"])}
             /\ aliasInstallation' = "none"
             /\ fingerprint' = IF Fault = "OldFingerprint" THEN fingerprint ELSE grants'
             /\ lastAction' = "reassign"
             /\ UNCHANGED <<expired, generation, phase, readRepo, readHome,
                             readInstallation, readScope, readGeneration,
                             requested, returned, returnedUsable,
                             bBefore, bExpiredBefore, aBefore,
                             workingInstallation, chunkPhase, mintedChunks,
                             inventoryNames, vaultNames>>
\* Proposed mid-session grant in B. Scope and inventory must be re-minted from
\* all B grants, while A's token stays intact. Current prepare_app_access
\* groups IDs at github_app_session.py:401-452 and records them at :428-440.
AddRepoB == /\ "c" \notin grants["B"] /\ generation["B"] = 1
            /\ grants' = [grants EXCEPT !["B"] = @ \cup {"c"}]
            /\ scope' = IF Fault = "GrantStaleScope" THEN scope
                         ELSE [scope EXCEPT !["B"] = grants'["B"]]
            /\ generation' = [generation EXCEPT !["B"] = 2]
            /\ live' = IF Fault = "GrantTouchesA" THEN [A |-> FALSE, B |-> TRUE]
                        ELSE [live EXCEPT !["B"] = TRUE]
            /\ expired' = [expired EXCEPT !["B"] = FALSE]
            /\ invalidated' = invalidated \cup {Token("B", 1)}
            /\ fingerprint' = IF Fault = "GrantOldFingerprint" THEN fingerprint ELSE grants'
            /\ aBefore' = live["A"] /\ lastAction' = "addB"
            /\ UNCHANGED <<home, phase, readRepo, readHome, readInstallation,
                            readScope, readGeneration, requested, returned,
                            returnedUsable, bBefore, bExpiredBefore,
                            workingInstallation, aliasInstallation, chunkPhase,
                            mintedChunks, inventoryNames, vaultNames>>
\* Per-token expiry and rotation; actual conservative expiry is 55 minutes
\* (github_app_session.py:428-440); rotation begins at :909-969.
Expire(i) == /\ i \in Installations /\ live[i]
             /\ live' = IF Fault = "ExpireAll" /\ i = "A"
                         THEN [A |-> FALSE, B |-> FALSE]
                         ELSE [live EXCEPT ![i] = FALSE]
             /\ expired' = IF Fault = "ExpireAll" /\ i = "A"
                            THEN [A |-> TRUE, B |-> TRUE]
                            ELSE [expired EXCEPT ![i] = TRUE]
             /\ invalidated' = invalidated \cup {Token(i, generation[i])}
                                 \cup (IF Fault = "ExpireAll" /\ i = "A"
                                       THEN {Token("B", generation["B"])} ELSE {})
             /\ aliasInstallation' = IF aliasInstallation = i THEN "none"
                                      ELSE aliasInstallation
             /\ lastAction' = IF i = "A" THEN "expireA" ELSE "expireB"
             /\ bBefore' = live["B"] /\ bExpiredBefore' = expired["B"]
             /\ UNCHANGED <<home, grants, scope, generation, fingerprint, phase,
                             readRepo, readHome, readInstallation, readScope,
                             readGeneration, requested, returned, returnedUsable, aBefore,
                             workingInstallation, chunkPhase, mintedChunks,
                             inventoryNames, vaultNames>>
Rotate(i) == /\ i \in Installations /\ grants[i] # {} /\ generation[i] = 1
             /\ generation' = IF Fault = "RotationStaleB" /\ i = "B"
                               THEN generation ELSE [generation EXCEPT ![i] = 2]
             /\ live' = IF Fault = "RotationStaleB" /\ i = "B"
                         THEN live ELSE [live EXCEPT ![i] = TRUE]
             /\ expired' = IF Fault = "RotationStaleB" /\ i = "B"
                            THEN expired ELSE [expired EXCEPT ![i] = FALSE]
             /\ scope' = IF Fault = "RotationStaleB" /\ i = "B"
                          THEN scope ELSE [scope EXCEPT ![i] = grants[i]]
             /\ invalidated' = IF Fault = "RotationStaleB" /\ i = "B"
                                THEN invalidated ELSE invalidated \cup {Token(i, 1)}
             /\ aliasInstallation' = IF Fault = "AliasFallback" /\ i = "B"
                                           /\ workingInstallation = "none"
                                      THEN "B" ELSE aliasInstallation
             /\ lastAction' = IF i = "A" THEN "rotateA" ELSE "rotateB"
             /\ UNCHANGED <<home, grants, fingerprint, phase, readRepo, readHome,
                             readInstallation, readScope, readGeneration,
                             requested, returned, returnedUsable,
                             bBefore, bExpiredBefore, aBefore,
                             workingInstallation, chunkPhase, mintedChunks,
                             inventoryNames, vaultNames>>
\* A session without a working repository has no GH_TOKEN or Copilot alias.
\* A B-only rotation must carry this choice from the full grant set.
ClearWorking == /\ Fault \in {"AliasScenario", "AliasFallback"}
                /\ workingInstallation = "A"
                /\ workingInstallation' = "none" /\ aliasInstallation' = "none"
                /\ lastAction' = "clearWorking"
                /\ UNCHANGED <<home, grants, scope, live, expired, generation,
                                fingerprint, invalidated, phase, readRepo, readHome,
                                readInstallation, readScope, readGeneration, requested,
                                returned, returnedUsable, bBefore, bExpiredBefore,
                                aBefore, chunkPhase, mintedChunks,
                                inventoryNames, vaultNames>>
\* Three chunks stand for one installation's sorted repository-ID chunks.
\* A failed first chunk must discard every chunk for that installation.
FailChunkZero == /\ Fault \in {"AtomicScenario", "PartialMint", "RenumberNames"}
                 /\ chunkPhase = "ready"
                 /\ chunkPhase' = "failed"
                 /\ mintedChunks' = IF Fault = "AtomicScenario" THEN {} ELSE {"1","2"}
                 /\ inventoryNames' = IF Fault = "AtomicScenario"
                                       THEN inventoryNames
                                       ELSE [n \in Chunks |-> IF n = "0" THEN "none" ELSE n]
                 /\ vaultNames' = inventoryNames'
                 /\ lastAction' = "mintFail"
                 /\ UNCHANGED <<home, grants, scope, live, expired, generation,
                                 fingerprint, invalidated, phase, readRepo, readHome,
                                 readInstallation, readScope, readGeneration, requested,
                                 returned, returnedUsable, bBefore, bExpiredBefore,
                                 aBefore, workingInstallation, aliasInstallation>>
MintAllChunks == /\ Fault = "AtomicScenario" /\ chunkPhase = "ready"
                 /\ chunkPhase' = "complete" /\ mintedChunks' = Chunks
                 /\ inventoryNames' = [n \in Chunks |-> n]
                 /\ vaultNames' = inventoryNames'
                 /\ lastAction' = "mintAll"
                 /\ UNCHANGED <<home, grants, scope, live, expired, generation,
                                 fingerprint, invalidated, phase, readRepo, readHome,
                                 readInstallation, readScope, readGeneration, requested,
                                 returned, returnedUsable, bBefore, bExpiredBefore,
                                 aBefore, workingInstallation, aliasInstallation>>
\* B-only rotation reconstructs A's inventory and skips unchanged vault names.
\* Renumbering surviving chunks makes name 1 point to the wrong token.
RebuildAfterBRotation == /\ chunkPhase \in {"failed", "complete"}
                         /\ chunkPhase' = "rotated"
                         /\ inventoryNames' = IF mintedChunks = {} \/ mintedChunks = Chunks
                                               THEN inventoryNames
                                               ELSE [n \in Chunks |->
                                                     IF n = "0" THEN "1"
                                                     ELSE IF n = "1" THEN "2" ELSE "none"]
                         /\ vaultNames' = IF Fault = "RenumberNames"
                                           THEN [n \in Chunks |->
                                                 IF n = "0" THEN "1"
                                                 ELSE IF n = "1" THEN "1" ELSE "none"]
                                           ELSE inventoryNames'
                         /\ lastAction' = "rebuild"
                         /\ UNCHANGED <<home, grants, scope, live, expired, generation,
                                         fingerprint, invalidated, phase, readRepo,
                                         readHome, readInstallation, readScope,
                                         readGeneration, requested, returned,
                                         returnedUsable, bBefore, bExpiredBefore,
                                         aBefore, workingInstallation,
                                         aliasInstallation, mintedChunks>>
OldNext == (\E r \in Repos: Read(r)) \/ Return \/ RevokeA \/ ReassignA \/ AddRepoB
        \/ (\E i \in Installations: Expire(i) \/ Rotate(i))
        \/ ClearWorking \/ FailChunkZero \/ MintAllChunks \/ RebuildAfterBRotation
\* The following actions abstract one inventory chunk or one vault request at
\* a time. They are gated to keep the older model's state counts unchanged.
RevokeAChunk == /\ chunkAction = "init"
                /\ chunkLive' = chunkLive \ {"A1"}
                /\ chunkAction' = "revoked"
                /\ UNCHANGED <<chunkDesired, chunkAge, chunkAgeBefore,
                                detectedChunks, ageDue, deliveryPhase, vaultCredentials,
                                singleRowsUnlabelled>>
DetectChunks == /\ chunkAction = "revoked"
                /\ detectedChunks' = IF Fault = "AnyRowDetection"
                                      THEN {} ELSE chunkDesired \ chunkLive
                /\ chunkAction' = "detected"
                /\ UNCHANGED <<chunkLive, chunkDesired, chunkAge, chunkAgeBefore,
                                ageDue, deliveryPhase, vaultCredentials,
                                singleRowsUnlabelled>>
\* Snapshot still records A after the sweeper removes its live inventory rows.
SweepContractA == /\ chunkAction = "init"
                  /\ chunkLive' = {"B0"}
                  /\ chunkDesired' = {"B0"}
                  /\ chunkAction' = "swept"
                  /\ UNCHANGED <<chunkAge, chunkAgeBefore, detectedChunks,
                                  ageDue, deliveryPhase, vaultCredentials,
                                  singleRowsUnlabelled>>
DetectSnapshotContraction == /\ chunkAction = "swept"
                             /\ detectedChunks' = IF Fault = "SweptContractionMissed"
                                                   THEN chunkLive \ chunkDesired
                                                   ELSE {"A0", "A1", "B0"} \ chunkDesired
                             /\ chunkAction' = "contractionDetected"
                             /\ UNCHANGED <<chunkLive, chunkDesired, chunkAge,
                                             chunkAgeBefore, ageDue, deliveryPhase,
                                             vaultCredentials, singleRowsUnlabelled>>
\* A multi-to-single refresh re-mints B into main's unlabelled row shape.
TransitionToSingle == /\ chunkAction = "init"
                      /\ chunkLive' = {"B0"} /\ chunkDesired' = {"B0"}
                      /\ singleRowsUnlabelled' = (Fault # "TransitionKeepsLabels")
                      /\ chunkAction' = "single"
                      /\ UNCHANGED <<chunkAge, chunkAgeBefore, detectedChunks,
                                      ageDue, deliveryPhase, vaultCredentials>>
ContractAChunks == /\ chunkAction = "init"
                   /\ chunkLive' = {"A0", "B0", "B1"}
                   /\ chunkDesired' = {"A0", "B0", "B1"}
                   /\ chunkAgeBefore' = chunkAge
                   /\ chunkAge' = IF Fault = "ResetAgeOnContraction"
                                  THEN [chunkAge EXCEPT !["B0"] = 0, !["B1"] = 0]
                                  ELSE chunkAge
                   /\ chunkAction' = "contracted"
                   /\ UNCHANGED <<detectedChunks, ageDue, deliveryPhase,
                                   vaultCredentials, singleRowsUnlabelled>>
TickChunks == /\ chunkAction = "contracted"
              /\ chunkAge' = [chunkAge EXCEPT !["B0"] = @ + 1, !["B1"] = @ + 1]
              /\ chunkAction' = "ticked"
              /\ UNCHANGED <<chunkLive, chunkDesired, chunkAgeBefore,
                              detectedChunks, ageDue, deliveryPhase, vaultCredentials,
                              singleRowsUnlabelled>>
DetectDue == /\ chunkAction = "ticked"
             /\ ageDue' = \E c \in chunkLive: chunkAge[c] >= 40
             /\ chunkAction' = "ageDetected"
             /\ UNCHANGED <<chunkLive, chunkDesired, chunkAge, chunkAgeBefore,
                             detectedChunks, deliveryPhase, vaultCredentials,
                             singleRowsUnlabelled>>
DeleteObsolete == /\ deliveryPhase = "delete"
                  /\ vaultCredentials' = vaultCredentials \ {"alias", "copilot"}
                  /\ deliveryPhase' = "update"
                  /\ chunkAction' = "deleted"
                  /\ UNCHANGED <<chunkLive, chunkDesired, chunkAge,
                                  chunkAgeBefore, detectedChunks, ageDue,
                                  singleRowsUnlabelled>>
UpdateExisting == /\ deliveryPhase = "update"
                  /\ deliveryPhase' = "create"
                  /\ chunkAction' = "updated"
                  /\ UNCHANGED <<chunkLive, chunkDesired, chunkAge, chunkAgeBefore,
                                  detectedChunks, ageDue, vaultCredentials,
                                  singleRowsUnlabelled>>
CreateNew(n) == /\ n \in {"13", "14"} /\ n \notin vaultCredentials
                /\ (deliveryPhase = "create"
                    \/ (Fault = "CreateBeforeDelete" /\ deliveryPhase = "delete"))
                /\ vaultCredentials' = vaultCredentials \cup {n}
                /\ deliveryPhase' = deliveryPhase
                /\ chunkAction' = "created"
                /\ UNCHANGED <<chunkLive, chunkDesired, chunkAge, chunkAgeBefore,
                                detectedChunks, ageDue, singleRowsUnlabelled>>
ChunkNext == IF Fault \in {"ChunkSafe", "AnyRowDetection"}
             THEN RevokeAChunk \/ DetectChunks
             ELSE IF Fault \in {"SnapshotContraction", "SweptContractionMissed"}
                  THEN SweepContractA \/ DetectSnapshotContraction
             ELSE IF Fault \in {"ModeTransitionSafe", "TransitionKeepsLabels"}
                  THEN TransitionToSingle
             ELSE IF Fault \in {"AgeSafe", "ResetAgeOnContraction"}
                  THEN ContractAChunks \/ TickChunks \/ DetectDue
                  ELSE DeleteObsolete \/ UpdateExisting
                       \/ (\E n \in {"13", "14"}: CreateNew(n))
Next == IF Fault \in NewFaults
        THEN ChunkNext /\ UNCHANGED oldvars
        ELSE OldNext /\ UNCHANGED chunkvars
Spec == Init /\ [][Next]_vars
TypeOK == /\ home \in [Repos -> Installations]
          /\ grants \in [Installations -> SUBSET Repos]
          /\ scope \in [Installations -> SUBSET Repos]
          /\ live \in [Installations -> BOOLEAN]
          /\ expired \in [Installations -> BOOLEAN]
          /\ generation \in [Installations -> {1,2}]
          /\ fingerprint \in [Installations -> SUBSET Repos]
          /\ invalidated \subseteq TokenIds
          /\ phase \in {"idle", "pending"}
          /\ readRepo \in Repos \cup {"none"}
          /\ readHome \in Installations \cup {"none"}
          /\ readInstallation \in Installations \cup {"none"}
          /\ readScope \subseteq Repos
          /\ readGeneration \in {0,1,2}
          /\ requested \in Repos \cup {"none"}
          /\ returned \in Installations \cup {"none"}
          /\ returnedUsable \in BOOLEAN
          /\ lastAction \in {"init","read","return","revokeA","reassign",
                              "addB","expireA","expireB","rotateA","rotateB",
                              "clearWorking","mintFail","mintAll","rebuild"}
          /\ bBefore \in BOOLEAN /\ bExpiredBefore \in BOOLEAN /\ aBefore \in BOOLEAN
          /\ workingInstallation \in Installations \cup {"none"}
          /\ aliasInstallation \in Installations \cup {"none"}
          /\ chunkPhase \in {"ready", "failed", "complete", "rotated"}
          /\ mintedChunks \subseteq Chunks
          /\ inventoryNames \in [Chunks -> Chunks \cup {"none"}]
          /\ vaultNames \in [Chunks -> Chunks \cup {"none"}]
          /\ chunkLive \subseteq ChunkSlots
          /\ chunkDesired \subseteq ChunkSlots
          /\ chunkAge \in [ChunkSlots -> 0..40]
          /\ chunkAgeBefore \in [ChunkSlots -> 0..40]
          /\ detectedChunks \subseteq ChunkSlots
          /\ ageDue \in BOOLEAN
          /\ chunkAction \in {"init", "revoked", "detected", "swept", "single",
                              "contractionDetected", "contracted",
                              "ticked", "ageDetected", "deleted", "updated", "created"}
          /\ deliveryPhase \in {"delete", "update", "create"}
          /\ vaultCredentials \subseteq {"1", "2", "3", "4", "5", "6", "7",
                                        "8", "9", "10", "11", "12", "13", "14",
                                        "alias", "copilot", "other"}
          /\ singleRowsUnlabelled \in BOOLEAN
ExactLiveScope == \A i \in Installations:
                    live[i] => scope[i] = grants[i]
                                /\ scope[i] \subseteq {r \in Repos: home[r] = i}
FreshRead == lastAction = "read" /\ readInstallation # "none"
             => live[readInstallation] /\ ~expired[readInstallation]
                /\ Token(readInstallation, readGeneration) \notin invalidated
ResultScoped == lastAction = "return" /\ returned # "none"
                => returned = readHome /\ requested \in readScope
GrantedReachable == lastAction = "read" /\ readRepo = "b"
                    /\ grants["B"] = {"b"} /\ ~expired["B"]
                    /\ generation["B"] = 1
                    /\ Token("B", 1) \notin invalidated
                    => readInstallation = "B"
NoStaleUsable == lastAction = "return" /\ returnedUsable
                 => returned # "none" /\ returned = home[requested]
                    /\ requested \in grants[returned]
                    /\ readGeneration = generation[returned]
                    /\ live[returned] /\ ~expired[returned]
UnrelatedSurvives == lastAction = "revokeA" => live["B"] = bBefore
ExpiryIsolation == lastAction = "expireA" =>
                   live["B"] = bBefore /\ expired["B"] = bExpiredBefore
RotationRefreshes == lastAction = "rotateB" =>
                     generation["B"] = 2 /\ live["B"] /\ ~expired["B"]
                     /\ scope["B"] = grants["B"]
GrantIsolation == lastAction = "addB" => live["A"] = aBefore
FingerprintExact == fingerprint = grants
AliasSelected == aliasInstallation = "none"
                 \/ (aliasInstallation = workingInstallation
                     /\ live[aliasInstallation] /\ ~expired[aliasInstallation])
AtomicChunks == chunkPhase = "ready" \/ mintedChunks = {} \/ mintedChunks = Chunks
StableNames == \A n \in Chunks: inventoryNames[n] = vaultNames[n]
MissingChunkDetected == chunkAction = "detected" /\ chunkLive # chunkDesired
                        => detectedChunks = chunkDesired \ chunkLive
SweptContractionDetected == chunkAction = "contractionDetected"
                            => detectedChunks = {"A0", "A1"}
ModeTransitionShape == chunkAction = "single" => singleRowsUnlabelled
RetainedTokenAge == chunkAction \in {"contracted", "ticked", "ageDetected"}
                    => chunkAge["B0"] >= chunkAgeBefore["B0"]
                       /\ chunkAge["B1"] >= chunkAgeBefore["B1"]
PerTokenDue == chunkAction = "ageDetected" => ageDue
VaultBudget == Cardinality(vaultCredentials) <= 15
=============================================================================
