------------------------------ MODULE ToolRules ------------------------------
\* Proposed per-agent tool rules. Question: can a gated tool call run without the
\* requester's approval when no admin-set allow rule was in the store when it was
\* decided; can a rule appear that no admin's successful click or edit made; and can
\* an "Always allow" click undo a revoke that committed after the click?
EXTENDS Naturals, FiniteSets

CONSTANTS
    UnsafeSnapshot,     \* decider reads rules once at turn start, not per call
    UnsafeAnyClicker,   \* "Always allow" accepted from a non-admin clicker
    UnsafeBlindUpsert,  \* rule save ignores the revision the card was posted at
    UnsafeSaveIfLost    \* click saves the rule even when its settlement lost (expiry or Deny won)

Calls == {1, 2}
Admins == {"admin"}
Clickers == {"requester", "admin"}
MaxPanel == 2

VARIABLES
    rule, ruleRev, ruleBy, ruleStale, ruleFromLost, \* store
    snap,                                           \* value read at turn start (UnsafeSnapshot)
    st, how, by, liveAtDecide, cardRev,             \* per call
    clicks,                                         \* admin checks in flight: [c, who]
    saves,                                          \* pending rule saves: [rev, by, stale, lost]
    panelOps
vars == <<rule, ruleRev, ruleBy, ruleStale, ruleFromLost, snap, st, how, by,
          liveAtDecide, cardRev, clicks, saves, panelOps>>

Init ==
    /\ rule \in {"none", "allow"} /\ ruleRev = 0
    /\ ruleBy = IF rule = "allow" THEN "admin" ELSE "nobody"
    /\ ruleStale = FALSE /\ ruleFromLost = FALSE
    /\ snap = rule
    /\ st = [c \in Calls |-> "new"] /\ how = [c \in Calls |-> "none"]
    /\ by = [c \in Calls |-> "nobody"]
    /\ liveAtDecide = [c \in Calls |-> "none"] /\ cardRev = [c \in Calls |-> 0]
    /\ clicks = {} /\ saves = {} /\ panelOps = 0

Store == <<rule, ruleRev, ruleBy, ruleStale, ruleFromLost>>
Seen == IF UnsafeSnapshot THEN snap ELSE rule

\* interactive_decider: read the rule, then allow or post a card (one await-free step)
Decide(c) ==
    /\ st[c] = "new"
    /\ liveAtDecide' = [liveAtDecide EXCEPT ![c] = rule]
    /\ IF Seen = "allow"
          THEN /\ st' = [st EXCEPT ![c] = "run"] /\ how' = [how EXCEPT ![c] = "rule"]
               /\ UNCHANGED cardRev
          ELSE /\ st' = [st EXCEPT ![c] = "card"]
               /\ cardRev' = [cardRev EXCEPT ![c] = ruleRev]
               /\ UNCHANGED how
    /\ UNCHANGED <<Store, snap, by, clicks, saves, panelOps>>

\* settle_confirmation: requester Approve, Deny, or expiry (single-use future)
Approve(c) ==
    /\ st[c] = "card"
    /\ st' = [st EXCEPT ![c] = "run"] /\ how' = [how EXCEPT ![c] = "requester"]
    /\ by' = [by EXCEPT ![c] = "requester"]
    /\ UNCHANGED <<Store, snap, liveAtDecide, cardRev, clicks, saves, panelOps>>
Refuse(c) ==
    /\ st[c] = "card"
    /\ st' = [st EXCEPT ![c] = "refused"]
    /\ UNCHANGED <<Store, snap, how, by, liveAtDecide, cardRev, clicks, saves, panelOps>>

\* "Always allow" click: the admin role check awaits (requester_role), so the click
\* starts here and finishes in a later step; expiry or Deny can win in between.
ClickStart(c, who) ==
    /\ st[c] = "card"
    /\ clicks' = clicks \cup {[c |-> c, who |-> who]}
    /\ UNCHANGED <<Store, snap, st, how, by, liveAtDecide, cardRev, saves, panelOps>>

ClickFinish(k) ==
    /\ k \in clicks
    /\ clicks' = clicks \ {k}
    /\ IF ~UnsafeAnyClicker /\ k.who \notin Admins
          THEN UNCHANGED <<st, how, by, saves>>               \* private refusal
          ELSE IF st[k.c] = "card"
             THEN /\ st' = [st EXCEPT ![k.c] = "run"] /\ how' = [how EXCEPT ![k.c] = "always"]
                  /\ by' = [by EXCEPT ![k.c] = k.who]
                  /\ saves' = saves \cup {[rev |-> cardRev[k.c], by |-> k.who, stale |-> FALSE, lost |-> FALSE]}
             ELSE /\ UNCHANGED <<st, how, by>>               \* settlement lost
                  /\ saves' = IF UnsafeSaveIfLost
                                 THEN saves \cup {[rev |-> cardRev[k.c], by |-> k.who, stale |-> FALSE, lost |-> TRUE]}
                                 ELSE saves
    /\ UNCHANGED <<Store, snap, liveAtDecide, cardRev, panelOps>>

\* The rule store's write: compare-and-set on the revision the card was posted at
CommitSave(s) ==
    /\ s \in saves
    /\ saves' = saves \ {s}
    /\ IF UnsafeBlindUpsert \/ s.rev = ruleRev
          THEN /\ rule' = "allow" /\ ruleRev' = ruleRev + 1
               /\ ruleBy' = s.by /\ ruleStale' = s.stale /\ ruleFromLost' = s.lost
          ELSE UNCHANGED Store
    /\ UNCHANGED <<snap, st, how, by, liveAtDecide, cardRev, clicks, panelOps>>

\* CLI or daimon-mcp edit by an admin. Clearing bumps the revision even when no rule
\* exists, so a pending save from before it cannot land.
PanelAllow ==
    /\ panelOps < MaxPanel
    /\ rule' = "allow" /\ ruleRev' = ruleRev + 1 /\ ruleBy' = "admin"
    /\ ruleStale' = FALSE /\ ruleFromLost' = FALSE
    /\ panelOps' = panelOps + 1
    /\ UNCHANGED <<snap, st, how, by, liveAtDecide, cardRev, clicks, saves>>
Clear ==
    /\ panelOps < MaxPanel
    /\ rule' = "none" /\ ruleRev' = ruleRev + 1 /\ ruleBy' = "nobody"
    /\ ruleStale' = FALSE /\ ruleFromLost' = FALSE
    /\ saves' = {[s EXCEPT !.stale = TRUE] : s \in saves}
    /\ panelOps' = panelOps + 1
    /\ UNCHANGED <<snap, st, how, by, liveAtDecide, cardRev, clicks>>

Next ==
    \/ \E c \in Calls : Decide(c) \/ Approve(c) \/ Refuse(c)
    \/ \E c \in Calls, w \in Clickers : ClickStart(c, w)
    \/ \E k \in clicks : ClickFinish(k)
    \/ \E s \in saves : CommitSave(s)
    \/ PanelAllow \/ Clear
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ rule \in {"none", "allow"}
    /\ \A c \in Calls : st[c] \in {"new", "card", "run", "refused"}

\* A call runs only on the requester's Approve, an admin's Always allow, or an allow
\* rule that was really in the store when the call was decided.
RunAuthorized ==
    \A c \in Calls : st[c] = "run" =>
        \/ (how[c] = "requester" /\ by[c] = "requester")
        \/ (how[c] = "always" /\ by[c] \in Admins)
        \/ (how[c] = "rule" /\ liveAtDecide[c] = "allow")
\* Only an admin ever sets an allow rule.
RuleByAdmin == rule = "allow" => ruleBy \in Admins
\* A click made before a clear never brings the rule back after it.
NoResurrection == rule = "allow" => ~ruleStale
\* A rule comes from a click only when that click actually approved its call.
SaveFollowsSettlement == rule = "allow" => ~ruleFromLost
\* Witness only: a call does run on a rule with no card.
NoRuleRun == \A c \in Calls : how[c] # "rule"
=============================================================================
