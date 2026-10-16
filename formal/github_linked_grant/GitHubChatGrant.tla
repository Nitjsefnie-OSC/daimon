----------------------------- MODULE GitHubChatGrant -----------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS IdentityCheck, CommitRecheck
ASSUME IdentityCheck \in BOOLEAN /\ CommitRecheck \in BOOLEAN

Users == {"manager", "member"}
GitHubUsers == {"gh1", "gh2"}
Agents == {"shared", "own"}
Repos == {"widgets", "other"}
None == "none"

VARIABLES phase, owner, opener, githubIdentity, proven, link, generation,
          saves, replayed, managers, covered, permitted, req, requester,
          agent, repo, checkedIdentity, checkedGeneration, checkedPermission,
          grant, foreignEver, unsafeEver, stalePermissionEver
vars == <<phase, owner, opener, githubIdentity, proven, link, generation,
          saves, replayed, managers, covered, permitted, req, requester,
          agent, repo, checkedIdentity, checkedGeneration, checkedPermission,
          grant, foreignEver, unsafeEver, stalePermissionEver>>

Init ==
  /\ phase = "unminted"
  /\ owner = None /\ opener = None /\ githubIdentity = None /\ proven = FALSE
  /\ link = [u \in Users |-> None]
  /\ generation = [u \in Users |-> 0]
  /\ saves = 0 /\ replayed = FALSE
  /\ managers = {<<"manager", "shared">>, <<"manager", "own">>,
                  <<"member", "own">>}
  /\ covered = Repos
  /\ permitted = Users \X Repos
  /\ req = "idle" /\ requester = None /\ agent = None /\ repo = None
  /\ checkedIdentity = None /\ checkedGeneration = 0
  /\ checkedPermission = FALSE /\ grant = FALSE
  /\ foreignEver = FALSE /\ unsafeEver = FALSE
  /\ stalePermissionEver = FALSE

Mint(u) ==
  /\ phase = "unminted" /\ u \in Users
  /\ phase' = "new" /\ owner' = u
  /\ UNCHANGED <<opener, githubIdentity, proven, link, generation, saves,
                  replayed, managers, covered, permitted, req, requester,
                  agent, repo, checkedIdentity, checkedGeneration,
                  checkedPermission, grant, foreignEver, unsafeEver,
                  stalePermissionEver>>

Open(u) ==
  /\ phase = "new" /\ u \in Users
  /\ phase' = "platform" /\ opener' = u
  /\ UNCHANGED <<owner, githubIdentity, proven, link, generation, saves,
                  replayed, managers, covered, permitted, req, requester,
                  agent, repo, checkedIdentity, checkedGeneration,
                  checkedPermission, grant, foreignEver, unsafeEver,
                  stalePermissionEver>>

PlatformCallback ==
  /\ phase = "platform"
  /\ (IdentityCheck => opener = owner)
  /\ phase' = "github" /\ proven' = (opener = owner)
  /\ UNCHANGED <<owner, opener, githubIdentity, link, generation, saves,
                  replayed, managers, covered, permitted, req, requester,
                  agent, repo, checkedIdentity, checkedGeneration,
                  checkedPermission, grant, foreignEver, unsafeEver,
                  stalePermissionEver>>

RejectForeign ==
  /\ phase = "platform" /\ IdentityCheck /\ opener # owner
  /\ phase' = "rejected"
  /\ UNCHANGED <<owner, opener, githubIdentity, proven, link, generation,
                  saves, replayed, managers, covered, permitted, req,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

GitHubCallback(g) ==
  /\ phase = "github" /\ g \in GitHubUsers
  /\ phase' = "used" /\ githubIdentity' = g
  /\ link' = [link EXCEPT ![owner] = g]
  /\ generation' = [generation EXCEPT ![owner] = @ + 1]
  /\ saves' = saves + 1
  /\ foreignEver' = (foreignEver \/ ~proven)
  /\ UNCHANGED <<owner, opener, proven, replayed, managers, covered,
                  permitted, req, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, unsafeEver,
                  stalePermissionEver>>

ReplayCallback ==
  /\ phase = "used" /\ ~replayed
  /\ replayed' = TRUE
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, managers, covered, permitted, req,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

Request(u, a, r) ==
  /\ req = "idle" /\ u \in Users /\ a \in Agents /\ r \in Repos
  /\ req' = "checking" /\ requester' = u /\ agent' = a /\ repo' = r
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, managers, covered, permitted,
                  checkedIdentity, checkedGeneration, checkedPermission,
                  grant, foreignEver, unsafeEver, stalePermissionEver>>

CheckPermission ==
  /\ req = "checking"
  /\ req' = "checked"
  /\ checkedIdentity' = link[requester]
  /\ checkedGeneration' = generation[requester]
  /\ checkedPermission' = (<<requester, repo>> \in permitted)
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, managers, covered, permitted,
                  requester, agent, repo, grant, foreignEver, unsafeEver,
                  stalePermissionEver>>

CanCommit ==
  /\ checkedIdentity # None /\ checkedPermission
  /\ (CommitRecheck =>
       <<requester, agent>> \in managers
       /\ link[requester] = checkedIdentity
       /\ generation[requester] = checkedGeneration
       /\ repo \in covered)

Commit ==
  /\ req = "checked" /\ CanCommit
  /\ req' = "done" /\ grant' = TRUE
  /\ unsafeEver' = (unsafeEver \/
       ~(<<requester, agent>> \in managers
         /\ link[requester] = checkedIdentity
         /\ generation[requester] = checkedGeneration
         /\ repo \in covered))
  /\ stalePermissionEver' = (stalePermissionEver \/
       (<<requester, repo>> \notin permitted))
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, managers, covered, permitted,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, foreignEver>>

RejectGrant ==
  /\ req = "checked" /\ ~CanCommit
  /\ req' = "denied"
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, managers, covered, permitted,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

RevokeManager(u, a) ==
  /\ req = "checked" /\ u = requester /\ a = agent
  /\ <<u, a>> \in managers
  /\ managers' = managers \ {<<u, a>>}
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, covered, permitted, req,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

Unlink(u) ==
  /\ req = "checked" /\ u = requester /\ link[u] # None
  /\ link' = [link EXCEPT ![u] = None]
  /\ generation' = [generation EXCEPT ![u] = @ + 1]
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, saves,
                  replayed, managers, covered, permitted, req, requester,
                  agent, repo, checkedIdentity, checkedGeneration,
                  checkedPermission, grant, foreignEver, unsafeEver,
                  stalePermissionEver>>

Rotate(u) ==
  /\ req = "checked" /\ u = requester /\ link[u] # None
  /\ generation[u] < 2
  /\ generation' = [generation EXCEPT ![u] = @ + 1]
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  saves, replayed, managers, covered, permitted, req,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

RemoveCoverage(r) ==
  /\ req = "checked" /\ r = repo /\ r \in covered
  /\ covered' = covered \ {r}
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, managers, permitted, req,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

RevokePermission(u, r) ==
  /\ req = "checked" /\ u = requester /\ r = repo
  /\ <<u, r>> \in permitted
  /\ permitted' = permitted \ {<<u, r>>}
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, replayed, managers, covered, req,
                  requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, grant, foreignEver,
                  unsafeEver, stalePermissionEver>>

Quiescent ==
  /\ req \in {"done", "denied"} /\ phase \in {"used", "rejected"}
  /\ UNCHANGED vars

Next ==
  \/ \E u \in Users: Mint(u) \/ Open(u) \/ Unlink(u) \/ Rotate(u)
  \/ PlatformCallback \/ RejectForeign
  \/ \E g \in GitHubUsers: GitHubCallback(g)
  \/ ReplayCallback
  \/ \E u \in Users, a \in Agents, r \in Repos: Request(u, a, r)
  \/ CheckPermission \/ Commit \/ RejectGrant
  \/ \E u \in Users, a \in Agents: RevokeManager(u, a)
  \/ \E r \in Repos: RemoveCoverage(r)
  \/ \E u \in Users, r \in Repos: RevokePermission(u, r)
  \/ Quiescent

Spec == Init /\ [][Next]_vars
NoForeignLink == ~foreignEver
GrantRestsOnCurrentAuthority == ~unsafeEver
SingleUseLink == saves <= 1
NoStalePermission == ~stalePermissionEver
RequestStep == CheckPermission
CommitOrReject == Commit \/ RejectGrant
EveryRequestResolves ==
  (req = "checking" \/ req = "checked") ~> (req = "done" \/ req = "denied")
LiveSpec == Spec /\ WF_vars(RequestStep) /\ WF_vars(CommitOrReject)
================================================================================
