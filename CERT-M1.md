# M1 certification: opt-in shared threads, 2026-10-12

**M1 gates PASS offline at integration `6acf162066f499bb0cc87167c8756babccb5e4f5`
(#862): M0 is certified (#828), C14 now runs an actual Anthropic → OpenAI
switch, and shared threads stay opt-in twice over with defaults byte-identical.**
No live calls were made. This commit changes documentation only.

All `lanes/` and `inbox/` evidence paths below are relative to the external
sprint archive `/home/clsandoval/cs/daimon-neutral-core-20261009`, not this
repository. Their SHA256 hashes pin the preserved bytes.

## What M1 is

| Piece | Merged | What it adds |
| --- | --- | --- |
| State store protocol | #487 | `mux.state`: bindings with generation CAS, idempotent operations, fenced leases, journal, usage outbox |
| Postgres state store | #538 | `daimon.core.stores.mux_state`, migration `0087_neutral_state` (bindings, journal, leases, config revisions) |
| Channel backend config | #598 | `daimon.core.channel_backend`: per-channel `ConfigRevision` chain, admission check before any provider call, refusal `backend_unsupported` |
| Shared threads | #632 | `daimon.core.shared_threads`: shared slot and owner, new-thread-only binding pinned to its revision, no caller credentials |
| C01 attribution test | #637 | each writer in a shared session is charged for their own turns |
| Golden turn-path parity | #539 | the 33 goldens compared on both turn paths |
| Anthropic offline conformance | #631 | Daimon's Anthropic port registered with the C01–C18 runner |
| C14 provider switch | #862 | an actual Anthropic → OpenAI channel switch through real admission, preparation and drivers |

**Opt-in only.** A channel shares a thread only when
`DAIMON_TURN__CHANNEL_BACKENDS` is on (default `false`) **and** its backend
configuration says `thread_mode="shared"`. Unconfigured channels keep today's
per-caller sessions byte for byte. `DAIMON_TURN__PATH` still defaults to `legacy`.

## Gates

Test paths are relative to the repository root. `test_shared_threads.py` is
`packages/core/tests/test_shared_threads.py`.

| Gate | Result | Evidence |
| --- | --- | --- |
| M0 prerequisite | PASS | `CERT-M0.md`, merged as #828 (`c4fe67252`). |
| C01: two humans, one workspace, caller-attributed charges | PASS | `::test_c01_each_writer_is_charged_for_their_own_turns_in_a_shared_session` (#637): alice's and bob's `usage_events` carry their own `platform_user_id` and tokens on one `managed_session_id`; `tenant_ledger` has exactly the two debits, and a replay adds nothing. `::test_c01_two_callers_share_one_workspace_with_their_own_attribution`: one MA session, owned by the thread, not either person; no account id in any MA request. `::test_c13_two_first_turns_in_a_new_shared_thread_share_one_session` races both first turns: one session, one binding. |
| C14: A→B config change: T1 stays on A, T2 on B; a missing A driver fails T1 visibly | PASS | `packages/core/tests/turn/test_c14_anthropic_to_openai.py::test_c14_anthropic_thread_retains_a_new_thread_runs_b_and_missing_a_refuses` (#862). Owned Postgres, real admission and preparation, real Anthropic and OpenAI drivers; only HTTP transports are mocked. A shared Anthropic thread T1 binds under revision 1. The channel switches to private OpenAI (revision 2), and T1 keeps its provider, revision, owner and session while a new T2 runs on OpenAI. Both run real host turns after the switch. Removing A's backend and registration makes T1 refuse with typed `AdmissionDenied(reason="backend_unsupported")`, with no further SDK request and both bindings unchanged. N4 review APPROVE, eval PASS; its mutation probes for a silent fallback and an ignored retained revision both fail the test. The pinning rule also holds within one provider: `::test_c14_a_thread_stays_on_the_revision_it_was_bound_under` and `::test_an_existing_thread_stays_per_caller_when_the_channel_starts_sharing`. |
| C15: migrate returns unsupported, binding unchanged | PASS | `::test_c15_migrate_is_unsupported_and_leaves_the_binding_unchanged`; `packages/mux/tests/drivers/anthropic/test_conformance.py::test_shared_probe_uses_exact_native_sdk_requests[C15]` and its migration mutant. |
| C16: extension bypass refused | PASS | `::test_c16_the_port_exposes_no_raw_client_and_refuses_undeclared_extensions`; `test_conformance.py::test_shared_probe_uses_exact_native_sdk_requests[C16]` and `::test_raw_sdk_leak_mutant_fails_shared_probe`. |
| Cross-tenant refusal | PASS | `::test_another_tenants_thread_with_the_same_ids_is_another_binding`; `::test_the_shared_owner_is_a_pure_function_of_the_thread_and_never_a_legacy_key`. The store refuses a foreign tenant's lease and journal append (`mux.state.suite` on Postgres, `packages/core/tests/stores/test_mux_state.py`). |
| Legacy private history never visible | PASS | `::test_legacy_private_history_is_never_the_shared_session`; `::test_the_owner_key_cannot_collide_on_separators`. |
| No caller credential in a shared workspace | PASS | `::test_c01_two_callers_…` (no account id in any MA request); `::test_a_shared_session_carries_no_personally_connected_server`; `::test_a_shared_thread_needs_an_app_mode_agent`. |
| SYS-047 access policy and SYS-048 protected channels enforced first | PASS | `::test_access_policy_and_protection_refuse_before_a_shared_thread_is_considered[…]`; the order is unchanged in `packages/core/tests/turn/test_admission.py` (`test_admit_refuses_an_invoker_outside_the_allowlist_before_any_ma_call`, `test_admit_refuses_a_turn_whose_reply_would_land_in_a_protected_channel`). |
| Default byte-identical | PASS | `tests/parity/test_ma_goldens.py` and `test_ma_call_ratchet.py` at the default flags: **84 passed**. With `channel_backends` forced on for every golden admission (50 counted): **74 passed**. `tests/golden` is unchanged. `::test_unconfigured_and_per_caller_binds_read_nothing_new` (no new reads for unconfigured or never-shared channels); `packages/core/tests/test_channel_backend.py::test_an_unconfigured_channel_resolves_to_anthropic_managed_agents_unchanged`. |

## Runs

Collected 2026-10-12 at `651920d4ceea8e9c68f919c603bec70d75e6a435`, #862's
head. Its tree is identical to integration `6acf162` (`git diff` is empty).
Every run used an owned, migrated test database with provider keys unset.

```text
uv run pytest -rA -n 2 packages/core/tests/test_shared_threads.py \
  packages/core/tests/test_channel_backend.py packages/core/tests/turn/test_admission.py \
  packages/mux/tests/drivers/anthropic/test_conformance.py \
  packages/core/tests/stores/test_mux_state.py packages/core/tests/test_migration_neutral_state.py \
  packages/core/tests/turn/test_c14_anthropic_to_openai.py
  -> 198 passed
uv run pytest -n 2 tests/parity/test_ma_goldens.py tests/parity/test_ma_call_ratchet.py
  -> 84 passed
PYTHONPATH=<plugin dir> U5A_COUNT_FILE=<count file> \
  PYTEST_ADDOPTS="-p channel_backends_on" uv run pytest -n 2 tests/parity/test_ma_goldens.py
  -> 74 passed; the count file holds 50 lines, one per admission with channel_backends on
```

`channel_backends_on` is a throwaway verification plugin, not checked in. It
wraps `daimon.core.turn.admission.admit_impl` to force `channel_backends=True`
and counts the admissions, because the golden scenarios build their own
`TurnDeps` and the environment variable alone does not reach them. Its bytes
are preserved in the archive (manifest below).

## Not certified here

These fail closed and belong to later work:

- **Live behaviour.** Every result above is offline. No provider was called.
- **OpenAI or Gemini shared threads.** C14 runs OpenAI per caller. Shared
  threads are certified on Anthropic only; the backend check admits
  `thread_mode="shared"` only for the Anthropic profile.
- **Adapter-side lookups** that still key on the caller (MCP `_session_gate`,
  `task_continuity`, `repo_binding`, Teams `commands.py`, Discord
  `continuation_dispatch`) find no session in a shared thread rather than
  another person's. MCP hub, CLI and scheduler binding to a shared thread is
  not done.
- **The usage sweep** cannot attribute a shared session's recovered usage to a
  person; it debits the tenant only (`docs/billing.md`). The per-turn recorder
  remains per writer.
- **No user surface** sets a channel's backend; only the core API exists.

## Evidence SHA256 manifest

| Preserved path | SHA256 |
| --- | --- |
| `lanes/N2-state/cert-m1/n2-cert-m1-core.log` | `21008c3ca946522848df20a691f215a83bc7a82c9b9681b2058fdcaafea47a13` |
| `lanes/N2-state/cert-m1/n2-cert-m1-goldens.log` | `95bdd90c9834fdda95cdd1121edbc9a31e34d42843bf4f6a6375793678e86a49` |
| `lanes/N2-state/cert-m1/n2-cert-m1-goldens-on.log` | `bc55ee228099366eea8ae69e38d15249a11929f1adbb06a5ce6ff68fedf93516` |
| `lanes/N2-state/cert-m1/channel-backends-on-admissions.txt` | `698de82692073ed428b811966702ae141dd347f47ca9f4c963f4cbe582f76908` |
| `lanes/N2-state/cert-m1/channel_backends_on.py` | `e4b766852b8977c4ff6f0e30e700b9a42f95bc518e3cad29fab977bf5f358d08` |
| `lanes/N4-turn/REVIEW-CERT-M1-C14-PROOF-651920d.md` | `5affc3c28ad45ed7d125326f08e9f30f02b488aa41beafb1a0b9f3392ead2fe6` |
| `lanes/N4-turn/EVAL-CERT-M1-C14-PROOF.md` | `aa46b2f9f164063ae69c646106d5c4c7fb8b7b59c5ac7301d7ee5bf71853815b` |
| `packages/core/tests/test_shared_threads.py` | `8b3a339ad85c581613236edaeeb5affa9ab0363e5aaff862482813ab467bb052` |
| `packages/core/tests/turn/test_c14_anthropic_to_openai.py` | `c5fb81df71449253b8a780bb5994ab8f66ed5ae775eaf9f1fca34a3153a2e525` |
