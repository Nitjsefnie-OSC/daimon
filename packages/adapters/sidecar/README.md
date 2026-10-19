# Daimon page-event sidecar

`daimon-sidecar` is a bounded HTTP event-source adapter. It depends on core and
runs no turns, model calls, source polls or MCP management tools. Core owns the
registration, signature/replay checks, evidence, durable wake and stop controls.
The sidecar has no direct table grants and receives a separate environment.

The application producer flag and durable database gate default off. Keep them
off until execution-bound origin grants and cleanup safety have passed review.
See `deploy/lanes/README.md` for the optional preview profile and constrained role
setup. No internal-watch migration or outside callback sender is included.

An authorized OS page host uses `daimon.core.continuity.page_events.sign_page_event`
after its own page ACL and CSRF checks. The HMAC SHA-256 message is UTF-8, with
newline-separated fields: `daimon-page-v1`, watch UUID, page UUID, key ID, Unix
seconds, event UUID, and the SHA-256 hex digest of the raw body. UUIDs use Python's
canonical lowercase spelling. Key IDs contain only ASCII letters, digits, `_`
and `-`. Random signing keys stay in secret management; only their hashes are
registered. A signature does not make page-authored text trusted instructions.

POST the body and signed headers to `/callbacks/watches/{watch_id}`. A 202 means
durable acceptance; 409 identifies an accepted event replay. Retries use the same
event UUID with a freshly signed timestamp. Every other path is private or absent.
Health checks do not enqueue anything. Core's queue is the only delivery queue.

Public v1 enrollment binds an existing Discord thread. All continuation adapters
read the reason, and the common driver gate rechecks watch bindings before sends.
Woken turns use a constrained read/report posture and never choose an owner,
agent, destination or instructions from callback fields. The SDK cannot remove
skills mid-session, so a session carrying skills is refused. Shell/native tools
and the Daimon management server are removed before sends. A changed provider
session requires explicit re-enrollment instead of implicit recovery.
