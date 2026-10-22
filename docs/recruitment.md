# Recruitment threads

Daimon can track a candidate in an existing Discord thread named
`!labs-staffing: Name`. Matching ignores case and leading whitespace. Enrollment
survives removing the prefix. This feature is disabled by default and runs
inside the existing Discord worker, core and scheduler, using the existing
Postgres database. Careers intake is not enabled by this feature.

## Operator setup

Apply migrations, configure encryption with `DAIMON_CRYPTO__KEYS`, and provision
the Discord tenant normally. Set `DAIMON_RECRUITMENT__ENABLED=true` on the Discord
worker and scheduler. Keep recruitment backend secrets out of public MCP,
sidecar and agent environments. These are service credentials, not user OAuth.

Set `DAIMON_RECRUITMENT__TENANTS` to a JSON object keyed by guild ID. Each entry
requires a nonempty recruitment parent-channel set and at least one recruiter:

```json
{"101":{"channel_ids":["2"],"recruiter_user_ids":["7"],"recruiter_role_ids":["42"],"enabled":true,"raw_retention_days":7,"retention_days":30}}
```

Use actual Discord IDs. Supply identical tenant settings to Discord, scheduler
and MCP; backend credentials go only to Discord and scheduler. Restart those
processes together when changing the channel boundary. There is no database
configuration table or recruitment configuration CLI. Configuration revisions
are derived from the settings to fence older approvals after a restart.

**Every recruitment parent channel and every thread under it is reserved for
human recruitment.** General agent mentions, organic classifiers, DM transfers,
Discord MCP reads, lists, search, attachment results and general posting tools
refuse these sources regardless of thread title, enrollment, processing enablement
or retention. Choose dedicated restricted parent channels, not a general chat
channel. A staffing prefix outside these parents does nothing. Empty tenant
settings disable the boundary with no privacy SQL or Discord lookups. An
unresolvable thread parent in a configured guild fails closed. Administrator
status grants no approval exemption.

For a real backend, set `DAIMON_RECRUITMENT__BACKENDS` to a JSON object keyed by
tenant UUID with `base_url` (HTTPS) and `api_key` for each tenant. Supply this
secret only to Discord and scheduler using your deployment's secret mechanism.
The backend must isolate its candidate results for that guild/credential pair.
Changing a backend binding or configuration revision pauses older approvals;
review outstanding requests before rotating credentials. Do not switch an
uncertain request to another backend.

The bot needs Message Content intent, View Channel, Read Message History and
Send Messages in Threads, plus membership in private threads. Use restricted
recruitment channels: in-thread cards are visible to everyone who can read the
thread. A thread that has already carried an agent session cannot be enrolled;
use a fresh candidate-only thread instead.

## Review and recovery

Create a named thread or rename an existing candidate-only thread. The scheduler
reads its starter and pages its history. It records message edits, including
older edits missed during downtime, by cycling through old history with a
persisted cursor. Initial confirmation is disabled until the first scan is
complete. More than 200 candidate messages or 60,000 source characters blocks
review; the bot does not confirm a truncated summary. Attachments contribute
links and filenames; Daimon does not download CVs.

The card shows a factual summary, uncertainty, possible matches and proposed
field changes. A configured recruiter with fresh access to the original thread
can create or link a candidate, correct details, or skip tracking. Corrections
are ordinary explicit thread messages followed by `/recruitment action:refresh`.
Every create, link and subsequent update requires explicit confirmation of the
current review revision and covered event watermark. Old clicks are refused and refresh the card. No hiring
ranking or automatic rejection is performed.

Confirmed writes enter a durable encrypted outbox before delivery. Timeouts and
retryable errors retain the same exact body and idempotency key, including after
restart. Exponential backoff honors `Retry-After`; five failed attempts require
manual retry. Failure cards show status and Retry/Discard controls. Only an
unsent or definitively rejected request can be discarded; uncertain requests
must first be reconciled with the original key. Backend manual-edit conflicts
are shown to the recruiter and do not trigger an automatic overwrite.

`/recruitment action:status` is an ephemeral, recruiter-only list of opaque thread
IDs and states. Use `action:retry` or `action:discard` with `thread_id` to recover
a paused item. Recovery checks live recruiter roles, original-thread access and
current service configuration. Revoked access or a missing, archived, locked or
unreachable thread pauses processing. Daimon never reopens it or moves candidate
data to a DM or another channel. Restore access to the original thread to retry.

Cards persist an opaque intent before posting and recover the message after a
lost response. Ambiguous or incomplete history lookup pauses posting rather
than duplicating a card. The existing Discord recovery loop repairs these cards
after restart; the scheduler does not open another gateway connection.

## Privacy and retention

Recruitment sources are withheld from ordinary/shared agent turns and the
existing Discord history, message, thread-list and search tools. Recruitment
adds no MCP or agent tools. Extraction is a stateless structured model request
with no tools, personal memory, agent memory or conversational provider session.
Raw source revisions and derived candidate state are encrypted at rest. Logs
contain operational identifiers and safe error codes, not candidate text.

Defaults retain raw source data for seven days and derived state/approved
payloads for thirty days after thread activity; both are configurable per tenant.
The scheduler erases expired data even when recruitment is disabled, retaining
minimal enrollment/idempotency tombstones to prevent replay. Settings keep
recruitment channels out of general agent reads independently of erasure. Expiry invalidates approval and retry. The Discord recovery loop also redacts expired cards while recruitment is
disabled, using the retained non-content message ID. Redacting
Discord cards requires the bot to retain access; Discord source messages and
backend records have their own retention policies.

## Synthetic Preview parity

Set `DAIMON_RECRUITMENT__SYNTHETIC=true` in the existing trusted processes for
synthetic QA only. This selects an in-process stateful fake HTTP transport and
a deterministic extractor: no recruitment API or model-provider calls occur.
It is never the default and needs no real recruitment key. Do not mix synthetic
and real candidate threads in the enabled tenant.

On the Preview bot in the QA guild, use a restricted synthetic channel and
configured test recruiter accounts:

1. Create or rename a fresh thread to `  !LABS-staffing: Synthetic candidate`.
   Wait for a complete review; correct, create or link using the current card.
   Verify that a staffing prefix outside the configured parent channels does
   not enroll, and that general mentions inside a recruitment parent/thread
   are ignored. From an ordinary channel, MCP reads/lists/search must withhold
   these sources without exposing names; `/dm-me move` must refuse before
   opening a DM or reading history.
2. Remove the prefix and post `Synthetic phone: 555-0100` (or
   `Synthetic position: Test engineer`). Verify an update diff appears and no
   update is delivered until Confirm update. Test a stale card and a
   non-recruiter click; both must be refused ephemerally.
3. Restart the worker/scheduler while a scan or card is pending, and edit an old
   message outside the recent window. Verify reconciliation changes the review.
4. Set `DAIMON_RECRUITMENT__SYNTHETIC_FAILURE_STATUS=503`, restart the trusted
   process using the setting and confirm a synthetic request. Verify visible
   backoff/failure. Restore zero and retry; the original key is retained.
5. Revoke the recruiter's role or private-thread access before a click/retry,
   and remove bot access to a thread. Check the opaque status remains visible
   to recruiters and candidate content never appears in another destination.
6. Capture sanitized cards and outbox state/key evidence, disable the feature
   and remove synthetic test data.

The fake deterministically reconstructs registration IDs from stable keys after
restart. Its in-memory journal is process-local; the durable OS outbox is the
cross-restart evidence. Automated tests additionally cover response loss after
application, replay deduplication and tenant separation. Synthetic Preview proves
OS behavior; real-backend contract parity remains a separate test against an
isolated recruitment endpoint, never a production endpoint.

## Source and delivery invariants

One pure settings predicate, `is_recruitment_channel(guild_id, channel_id,
parent_id)`, decides privacy at each non-recruitment entry before content is read.
Thread parents are immutable; titles and enrollment never authorize a reader.
There are no privacy database caches, task snapshots or per-message context
filters. Parent-channel histories, older CV scans, reply targets and thread
histories cannot be opened by the general agent because their source channel
is unavailable. The same boundary precedes classifier collection, queued turns,
DM moves and descriptive MCP errors.

Gateway creates, edits, deletes and bulk deletes in enrolled threads (including
parent starters) durably increment `seen_watermark` before Discord or model
work. Reviews record `covered_watermark`; cards bind it together with the summary
revision. Confirmation checks equality with the current counter in the same
transaction that inserts the approved outbox. Pending enrichment blocks delivery.
Known own-card IDs and own-author events do not enter the evidence ledger.
Unknown-author bot edits are durably classified during enrichment/reconciliation;
only their covered watermark advances, preserving an identical review.

Deletions are content-free tombstones. Raw retention preserves edit times,
hashes and a message manifest, so newer edits and offline deletions still
invalidate derived reviews without restoring unchanged erased text.

An outbox independently records whether any attempt may have applied. A later
rejected attempt cannot erase uncertainty from an earlier timeout. Only an
authoritative same-key outcome resolves it. The card and service share the same
discard predicate.
