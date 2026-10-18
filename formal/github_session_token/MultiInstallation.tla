-------------------------- MODULE MultiInstallation --------------------------
EXTENDS Naturals, FiniteSets
CONSTANT Fault
Installations == {"A", "B"}
Repos == {"a", "b", "c"}
Token(i, n) == <<i, n>>
TokenIds == Installations \X {1, 2}
VARIABLES home, grants, scope, live, expired, generation, fingerprint,
          invalidated, phase, readRepo, readHome, readInstallation,
          readScope, readGeneration, requested, returned, returnedUsable,
          lastAction, bBefore, bExpiredBefore, aBefore
vars == <<home, grants, scope, live, expired, generation, fingerprint,
          invalidated, phase, readRepo, readHome, readInstallation,
          readScope, readGeneration, requested, returned, returnedUsable,
          lastAction, bBefore, bExpiredBefore, aBefore>>

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
                           fingerprint, invalidated, bBefore, bExpiredBefore, aBefore>>
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
                          bBefore, bExpiredBefore, aBefore>>
\* Proposed per-installation hard-change invalidation. Grant removal is at
\* github_access.py:495-500; token-row recheck is github_issued_tokens.py:550-566.
RevokeA == /\ grants["A"] # {}
           /\ grants' = [grants EXCEPT !["A"] = {}]
           /\ scope' = [scope EXCEPT !["A"] = {}]
           /\ live' = IF Fault = "RevokeAll" THEN [A |-> FALSE, B |-> FALSE]
                       ELSE [live EXCEPT !["A"] = FALSE]
           /\ invalidated' = IF Fault = "NoUpstreamRevoke" THEN invalidated
                              ELSE invalidated \cup {Token("A", generation["A"])}
           /\ fingerprint' = IF Fault = "OldFingerprint" THEN fingerprint ELSE grants'
           /\ lastAction' = "revokeA" /\ bBefore' = live["B"]
           /\ UNCHANGED <<home, expired, generation, phase, readRepo, readHome,
                           readInstallation, readScope, readGeneration,
                           requested, returned, returnedUsable, bExpiredBefore, aBefore>>
\* A URL can remain the same while its installation changes. Current snapshot
\* stores only URLs: session_snapshot.py:143-145, session_compat.py:173-177.
ReassignA == /\ home["a"] = "A" /\ grants["A"] = {"a"}
             /\ home' = [home EXCEPT !["a"] = "B"]
             /\ grants' = [grants EXCEPT !["A"] = {}, !["B"] = @ \cup {"a"}]
             /\ scope' = [A |-> {}, B |-> {}]
             /\ live' = [A |-> FALSE, B |-> FALSE]
             /\ invalidated' = invalidated \cup
                                 {Token("A", generation["A"]), Token("B", generation["B"])}
             /\ fingerprint' = IF Fault = "OldFingerprint" THEN fingerprint ELSE grants'
             /\ lastAction' = "reassign"
             /\ UNCHANGED <<expired, generation, phase, readRepo, readHome,
                             readInstallation, readScope, readGeneration,
                             requested, returned, returnedUsable,
                             bBefore, bExpiredBefore, aBefore>>
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
                            returnedUsable, bBefore, bExpiredBefore>>
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
             /\ lastAction' = IF i = "A" THEN "expireA" ELSE "expireB"
             /\ bBefore' = live["B"] /\ bExpiredBefore' = expired["B"]
             /\ UNCHANGED <<home, grants, scope, generation, fingerprint, phase,
                             readRepo, readHome, readInstallation, readScope,
                             readGeneration, requested, returned, returnedUsable, aBefore>>
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
             /\ lastAction' = IF i = "A" THEN "rotateA" ELSE "rotateB"
             /\ UNCHANGED <<home, grants, fingerprint, phase, readRepo, readHome,
                             readInstallation, readScope, readGeneration,
                             requested, returned, returnedUsable,
                             bBefore, bExpiredBefore, aBefore>>
Next == (\E r \in Repos: Read(r)) \/ Return \/ RevokeA \/ ReassignA \/ AddRepoB
        \/ (\E i \in Installations: Expire(i) \/ Rotate(i))
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
                              "addB","expireA","expireB","rotateA","rotateB"}
          /\ bBefore \in BOOLEAN /\ bExpiredBefore \in BOOLEAN /\ aBefore \in BOOLEAN
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
=============================================================================
