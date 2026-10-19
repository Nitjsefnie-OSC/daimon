# Lead intake

Daimon can durably register consulting and project enquiries from Gmail or an
explicit Discord “register this lead” request. Enable the optional `lead-intake`
skill for classification and dossier preparation. There is no public web form,
historical import or email sending. The reply remains a labelled **DRAFT** inside
the lead's Discord thread.

## Operator setup

Apply database migrations and configure `DAIMON_CRYPTO__KEYS`. Lead evidence and
integration credentials use the deployment's MultiFernet key ring. Keep retired
keys available until their encrypted data has expired or been rotated.

Create two operator-owned, mode-0600 JSON files. Run:

```bash
daimon leads configure TENANT_UUID intake-config.json intake-credentials.json
```

Configuration contains the executing `agent_id` (the tenant-derived agent UUID),
`guild_id`, restricted sales `channel_id`, `source_connection_id`, actual
`gmail_mailbox`, `crm_connection_id`, HubSpot `portal_id`, recipient allowlist,
optional `routine_id`, notifier and resolver **user** IDs, pipeline, stage and
optional deal-field mappings. `reconciliation_start` is a timezone-aware cutover
checkpoint; choose the current time to start without historical import. Names,
addresses, portal IDs and CRM field mappings are deployment-specific.

An example configuration using synthetic identifiers:

```json
{
  "agent_id": "00000000-0000-0000-0000-000000000001",
  "guild_id": "100", "channel_id": "200",
  "source_connection_id": "sales-gmail",
  "gmail_mailbox": "mailbox@example.test",
  "crm_connection_id": "sales-hubspot", "portal_id": "300",
  "recipients": ["enquiries@example.test"],
  "notifier_user_ids": ["400"], "resolver_user_ids": ["400"],
  "reconciliation_start": "2026-10-19T00:00:00Z",
  "mode": "capture", "revision": 1
}
```

The credential file contains `hubspot_access_token` and either a temporary
`gmail_access_token`, or `gmail_refresh_token`, `gmail_client_id` and
`gmail_client_secret`. Never paste these into chat or mount them in the agent's
vault. The existing host Discord bot credential supplies Discord I/O.
The configure command stores files locally without contacting providers; portal
and unique-property validation happen before CRM processing.

Gmail needs profile/message/list access and modify authority for archive, with
no outbound email scope. HubSpot needs company/contact/deal data access and
associations. Pre-provision the unique string properties
`daimon_company_identity` (companies) and `daimon_intake_identity` (deals) with
separate schema-write authority. Runtime fails closed if uniqueness or portal
identity cannot be verified. Configure the pipeline/stage and optional fields
for your portal; intake does not overwrite existing business fields.

## Modes and cutover

The default mode is `capture`: verified submissions are stored, with no Discord,
CRM or archive writes. After checking configuration and stopping any previous
writer for the mailbox, re-run configure with `mode: active`. Keep the revision
for a mode-only change. Increase `revision` for changes to connections,
destinations or other configuration; records accepted under an earlier revision
stop for operator review rather than being redirected silently.

`crm_paused` continues posting dossiers and archiving email, while stopping CRM
processing. Use `capture` to stop every external effect. Changing the mode does
not delete accepted work. Do not run two independent intake systems against the
same mailbox and CRM.

## Qualification and delivery

The model classifies each discovered message from its original source. The
skill rejects cold sales pitches, SEO offers, newsletters, recruiting mail,
invoices, automatic replies, bounces, phishing, calendar invites, partnership
spam and course enquiries as `other`. Core rejects `other` deterministically
before any delivery, CRM or archive effect; it also verifies source identity,
actual mailbox, recipient match and literal original-enquirer/company evidence.
Research never establishes source identity. Ambiguous identity waits for review
before CRM processing, while the dossier can still be delivered.

Each accepted lead gets a title-only post in the configured restricted sales
channel and a thread holding the full dossier, research summary, useful
translation and draft reply. Only configured user IDs may be pinged. Roles and
`@everyone` cannot be pinged. Attachments are discarded. Gmail is archived only
after all dossier chunks have posted, independently of CRM completion.

Company domains, contact email and exact project descriptions are matched
conservatively. Duplicate or plausible conflicting records require a human
selection. Creates carry scoped stable remote identities; association writes
are read back. A timed-out write remains uncertain until recovery finds the
remote result. An empty search alone never authorizes another create.

## Tools and review

- `lead_intake_submit` verifies and records the immutable source. Replaying the
  same source returns the original intake and never replaces its accepted text.
- `lead_intake_resume` recovers pending effects. Without an ID it processes up to
  25 pending records and reconciles Gmail, including archived mail. Failed pages
  preserve discoveries without advancing the checkpoint. Repeated cursors fail
  closed. Resume/classify until both coverage and discovered work are clear.
- `lead_intake_resolve` records an authorized human's evidence and exact CRM
  selections, then resumes. Completed identities cannot be changed. An uncertain
  create must first be recovered; it cannot be replaced by selecting another ID.
- `lead_intake_get`, `lead_intake_list` and `lead_intake_health` provide private
  readback. List supports `after_id` pagination; health counts all records.

All tool access requires the configured agent, tenant and live Discord
membership, ordinary tenant invoker admission and access to the sales channel.
Manual sources must be the authenticated author's own explicit registration.
Isolated sources cannot be exported. Destination/category and agent policy are
rechecked before each effect, with no public fallback.

Resolution additionally requires a configured sales-resolver user or a current
core-role member. Server admin status alone does not grant resolution. With
`invoker_admin_bypass` off, an admin still needs ordinary invoker admission.
Only the named routine with a live server-issued execution grant can perform
scheduled submit/resume; routines never resolve. A successful routine turn is
not proof that a scan or lead completed. Inspect review/error state and alert
only authorized owners with opaque IDs and private links.

## Retention and deletion

Lead text, names, research, draft and resolution evidence are encrypted for at
most 90 days. Reconstructed raw email excludes attachments and expires after
seven days; operators can shorten both periods. The scheduler erases expired
content independently of mailbox activity; reads hide expired evidence immediately and resume also enforces erasure.
After expiry, only a tenant-scoped source digest and minimal ledger metadata
remain. This tombstone prevents the same email being processed again. Effects,
remote IDs and resolution audit are erased with the content.

Tenant deletion cascades through intake storage. Account privacy deletion erases
that actor's submitted lead evidence and associated local audit/effects while
keeping a replay tombstone. External enquirers can request deletion through the
workspace operator; intake is tenant business data, not personal memory.

Local erasure does not delete HubSpot or Discord copies, and database erasure
cannot remove historical backups immediately. Operators must apply their backup
expiry policy and separately authorize, perform and verify provider export or
deletion using the recorded remote identities before erasing local content.
Never claim provider copies were deleted based on a local purge. Lead content
is not automatically ingested into memory, cards or public artifacts.

Discord's `/leads` command shows an ephemeral pending queue to admitted members
who can view the configured restricted sales channel. `/leads intake_id:UUID`
shows private status and the dossier thread link. Review decisions continue in
an authorized conversation through the same core resolution contract.

For an uncertain Discord starter, an authorized resolver can select a verified
Daimon message ID, or confirm absence after at least ten minutes and a successful
candidate scan. Absence with remaining candidates cannot authorize a repost.

After reviewing changed connection/destination configuration, a deployment
operator can explicitly rebind unfinished work with
`daimon leads adopt-config TENANT_UUID INTAKE_UUID REVISION`. This records the
old/new configuration in encrypted audit, performs no provider writes, and
requires a separate authorized resume. Completed work cannot be rebound.

Connection/destination changes are refused while unfinished records have effect
intents. Finish or reconcile that work with its original configuration first.
Configuration adoption applies only to work whose effects have not started.

Recovery may replay only operations with native duplicate prevention after
remote reconciliation: CRM unique-key creates, one thread per starter, label-set
archive and association PUTs. Uncertain Discord message POSTs require history
recovery or authorized starter resolution; they are never blindly reposted.
