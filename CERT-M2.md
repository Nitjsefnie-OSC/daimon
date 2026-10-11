# OpenAI M2 partial certification — 2026-10-09

**Incomplete: 3 live shared-fixture PASS, 15 typed PENDING, 0 FAIL.**
This does not certify M2 or the whole `openai.persistent_workspace` profile.
The driver is opt-in and production defaults remain unchanged.

## Run and evidence

Recorded C10/C15/C16 used `gpt-6-luna`, driver
`a0ddd219fd8d1eb8852d7726ee38b6db0bb3f1e6`, after both exact-head reviews.
The remaining entries come from the initial resource driver
`824e629d9c89669c326f0f7a435dba62a5aa2700` and refused missing scenarios
before credential lookup or native requests. There are no fixture retries
beyond the lead-authorized attempts documented below.

[Tapes and audited receipts](tests/conformance/recordings/openai/receipts.json)
contain normalized recorder version 2 data only. Native IDs are stable path
pseudonyms; no native response, message, error body, secret, request values or
binary content is included. The merged recorder and credential audit are unchanged.
There are **zero normalized Events** in these idle-session tapes. Their offline
replay checks independently authored request routes, order, field names, redacted
headers and absence of unexpected writes. It cannot prove native response decoding,
SSE, binary fidelity, nonempty journal recovery or tool-argument content. Empty
PENDING tapes prove no live scenario and are never counted as PASS.

C01–C09 receipts were recovered from their existing settled N9 ledger entries
when the original manifest finalization was interrupted. Their empty tapes survived;
the typed PENDING results were reconstructed offline from the same declarations.
The manifest labels that reconstruction explicitly. No provider call was repeated.

## Historical fixture hashes and transport pins

These pins describe the **2026-10-09** partial run, not a new live certification.
The run's two driver commits above and the post-812 tree resolve `uv.lock` to
`openai==2.54.0` (constraint `>=2.54.0,<3`). The lockfile SHA-256 is
`e28314812edccff303b1a18b5c30b89486ba6f1010c3817ecbe6fd6df0752e9e`.
The reviewed transport pins `OpenAI-Beta: agents=v1`, SDK retries to zero and
the `/v1/agents` HTTP resource family. This header pin comes from the reviewed
transport source; the normalized request metadata tapes omit the beta header.
The historical official reference snapshots were fetched 2026-09-14 and checked
2026-10-09, as recorded by that driver README. The later 2026-10-10 documentation
audit does not retroactively change this run's evidence date or schema pin.

SHA-256 hashes below cover each committed normalized tape's exact bytes. A hash
for an empty PENDING tape identifies that declaration; it adds no live coverage.
`F1-scripted.json` is separate later evidence and is outside this C01-C18 certificate.

| Fixture tape | SHA-256 |
| --- | --- |
| [C01.json](tests/conformance/recordings/openai/C01.json) | `2e72d36e3a3247f8b4d0139406e30cca1ae8028bf92d8de67e52952d6c051dda` |
| [C02.json](tests/conformance/recordings/openai/C02.json) | `881137be04c3b072cdf711f8672121f7641409592cd2d5f0bf89a9cdba3fbd62` |
| [C03.json](tests/conformance/recordings/openai/C03.json) | `4832168b68df810df33038b860dde61a8f4e65961f4ffe56bab4bd27f394b90a` |
| [C04.json](tests/conformance/recordings/openai/C04.json) | `13ef501f9796317c67e519cf6cd169ee56bdf86a3f9623bf97237d1e481daed0` |
| [C05.json](tests/conformance/recordings/openai/C05.json) | `74fc46b3926bf7a8bcf59120a6918a2c2907624b16c09f2e6178d818d140b7ff` |
| [C06.json](tests/conformance/recordings/openai/C06.json) | `8de9725c931820db3b789b5e65909195988aee13e39b05e3c8dfcae1a310f779` |
| [C07.json](tests/conformance/recordings/openai/C07.json) | `ca1fe53eadac23fe027e59ef6edf6cc8ef9e03efa4b9d2d6aac5a83c028cb9d4` |
| [C08.json](tests/conformance/recordings/openai/C08.json) | `e1233c5e5faf889f35f1879dabdbb4d4d3dbaa767a583d6e88d6616a63fcf545` |
| [C09.json](tests/conformance/recordings/openai/C09.json) | `24378bab8c1591ee5d1354d92fcbbad37fb3bd9dde5b6c883d905a70e07175d9` |
| [C10.json](tests/conformance/recordings/openai/C10.json) | `71ebd3dac66e6b9a3697453ddf55fdab8ebd6bd86c17f0cbc42e71b27a309966` |
| [C11.json](tests/conformance/recordings/openai/C11.json) | `52c6d840bca8c53d6df5537a3b1570c3b34d0ef5c9a5fd35f349afdf83f9c793` |
| [C12.json](tests/conformance/recordings/openai/C12.json) | `62b4035938388e3b1bce74e8797a41bb8bf285a05ffaefb1367797c195fb7d22` |
| [C13.json](tests/conformance/recordings/openai/C13.json) | `f725d1326e95cc68d1c103111955175d6adc5d5e22a5f1aec4821b2db471b462` |
| [C14.json](tests/conformance/recordings/openai/C14.json) | `570146d5ad0f843096fa5467509fca575bb764880b1a8ad630f56b5b271c8697` |
| [C15.json](tests/conformance/recordings/openai/C15.json) | `816f0e24a6ecd93bd3238339384b18471be8a7d7bd67f16d98d2dab6ed7387f2` |
| [C16.json](tests/conformance/recordings/openai/C16.json) | `1f1d9d34368f4e61c6d315d3cb88b5bec295924c90f523e3c8b7692d461cc9d2` |
| [C17.json](tests/conformance/recordings/openai/C17.json) | `39fe48072b6d8b8c07fbdcfb43a33f22754572b3d85fbb1289a7291d10387d51` |
| [C18.json](tests/conformance/recordings/openai/C18.json) | `ae46be726577b1f6d365d2ed3e796d21da037c71bafd69a7285a85e14465663c` |

The unchanged [receipt manifest](tests/conformance/recordings/openai/receipts.json)
has SHA-256 `15984d8fa933f0f51f281ec098599ebd8d0076493c052f54318e65da7eda385e`.

## Fixture matrix

| Fixture | Live result | Evidence or gap | Settled run receipt |
| --- | --- | --- | --- |
| C01 | PENDING | host attribution, batching and workspace binding adapter not wired | `2cc4138ad882425fb76a57a0464e9880` |
| C02 | PENDING | native hosted expiry classification and host session preparation unverified | `27513346958949f984c6441b302c5daa` |
| C03 | PENDING | host intent, atomic send claiming and restart receipt recovery not injected | `a7b6da712d21473a909f9c4a48103d6f` |
| C04 | PENDING | host lease/fencing and transactional journal crash-recovery adapter are not injected | `8ab5b2a9aae048c69de2ba4d546eb280` |
| C05 | PENDING | live adapter cannot arrange child-first completion, exact root/item aliases and deterministic disconnect/pagination faults | `df0cee47be8549428f5633ae543d6e9a` |
| C06 | PENDING | shared probe requires root alias and deterministic pre/post-cancel observation barrier; lifecycle smoke does not establish the EOF assertion | `3ef534cf5ae0485b972c9dbca92b4d02` |
| C07 | PENDING | current native usage snapshots need a shared revision/outbox StateStore adapter | `2ba12c739c8e42a98928f433677d5246` |
| C08 | PENDING | conditional required-mount replacement refused; host preparation adapter absent | `161ac9a7dd434a2bbc74e008c88f9949` |
| C09 | PENDING | live publication of two exact binary artifacts plus shared vault and download interruption is not arranged; normalized tapes cannot certify binary fidelity | `1d1d4d24ed724a4193335c013586cdc3` |
| C10 | PASS | unknown/unsupported requirements and foreign refs refused before mutation; invalid extension version refused; cleanup recovered HTTP 409 | `cc07c165d28f4146a6a126b162839bcb` |
| C11 | PENDING | deployed repository/MCP bindings and separate display titles are unavailable | `bfa7be86194d4bf58ce40245ccea79c5` |
| C12 | PENDING | billing identity and overlapping-grain pricing need the host certification hook | `f99649329ce6472192b1fb9f75141cdc` |
| C13 | PENDING | host new-slot binding adoption and restart persistence are not injected | `557b33ad04384b78a7d1f941debe12b6` |
| C14 | PENDING | existing/new-thread provider registry selection needs the Daimon host adapter | `e91b6d81f8304b69b7a8e3f975258724` |
| C15 | PASS | migration typed unsupported, binding unchanged, no provider writes; cleanup recovered HTTP 409 | `69b405ee240f4d089f378c1d2b72d013` |
| C16 | PASS | no public raw handle, undeclared namespace/native-input bypass refused before writes; cleanup recovered HTTP 409 | `13d0b69a3cfd4b1a9de10050c67a58c9` |
| C17 | PENDING | host wake-generation fencing is not exercised by provider event normalization | `098dab665bad4586a185953a3ac47c59` |
| C18 | PENDING | host termination/outcome-row mapping is not supplied; mux cannot import daimon | `b72d25dea7be409db460aea55eb65bf2` |

## Mandatory capability coverage

The existing core profile declaration rests on synthetic driver evidence accepted
by the lead for the resource slice. Full live continuity certification remains pending.

| Capability | Existing offline evidence | Live coverage still missing |
| --- | --- | --- |
| Workspace persistence | [Synthetic two-turn workflow](packages/mux/tests/drivers/openai/test_skill_workflow.py) reuses environment and immutable skill versions | C01/C04 hosted continuity and persisted original pins when provider metadata is lost |
| Turn lifecycle | [Offline C05](packages/mux/tests/drivers/openai/test_conformance.py): root/child outcomes, saved items and EOF occupancy | C05 controlled child-first/disconnect/pagination; the earlier lifecycle smoke covers one turn only |
| Cancel | [Offline C06](packages/mux/tests/drivers/openai/test_conformance.py): queued acknowledgement and independently observed interruption | C06 deterministic EOF barrier and occupancy assertion; the earlier smoke is a narrow observation |
| Tool loop | [Synthetic routing regression](packages/mux/tests/drivers/openai/test_driver.py), `test_tool_result_routes_call_and_turn_from_required_action` | A real required-action/output round trip |
| Required actions | [Synthetic exact nested-envelope regressions](packages/mux/tests/drivers/openai/test_recovery_usage.py), `test_recovery_uses_documented_nested_action_turn_identity` | Live function, browser and environment action scenarios |
| Skills bundle | [Actual-SDK synthetic workflow](packages/mux/tests/drivers/openai/test_skill_workflow.py): multipart upload, concrete immutable hosted pins and two turns | Live skill publication/install/use across turns |
| Artifacts | [Offline C09 and mutants](packages/mux/tests/drivers/openai/test_resource_conformance.py): binary checksum, pagination, interrupted download and shared-vault retention | C09 two real binary artifacts, shared vault and controlled interruption |
| Usage observations | [Synthetic usage regressions](packages/mux/tests/drivers/openai/test_recovery_usage.py): nullable counts, subagents and signed revisions | Provider usage receipt, attribution/pricing and C12 durable host accounting |
| Reconcile | [Offline C05](packages/mux/tests/drivers/openai/test_conformance.py) and [synthetic saved-state recovery](packages/mux/tests/drivers/openai/test_recovery_usage.py): overlap and terminal journal | Live stream disconnect, saved pagination, durable journal and explicit gap |

C10 tests rejection and scope boundaries. C15 verifies typed refusal of unsupported
migration and unchanged binding, rather than implemented migration. C16 checks the
raw-handle/extension boundary and mutation-free refusal against an idle journal.
They cannot substitute for the missing lifecycle/resource/host fixtures.
Rows supported only by synthetic regressions remain gaps against FOLLOWUPS' full
conformance-or-live evidence requirement; this partial certificate does not close it.

## Attempts, cleanup and budget

The first C10 attempt failed our recorder's opaque-path audit. A lead-authorized
rerun after path pseudonymization, and initial C15/C16, then failed immediate
session deletion with HTTP 409. Those failures and their full reserved holds remain
in the manifest's `prior_attempts`; they have not been relabelled PASS.

Two exact administrative cleanups removed the original C10 residual and then
three C10/C15/C16 residual sessions. The latter used five read requests and exactly
three deletes after fresh idle checks. Receipts remain in the sprint lane:
`cert-cleanup-20261009T223608Z.json` and
`cert-residual-cleanup-20261009T224259Z.json`.
The reviewed driver fix now waits for idle/failed, rechecks scope and identity,
recovers only definite 409 rejections, preserves the deletion key, and bounds
cleanup at 30 seconds. An interrupted DELETE reports an unknown outcome.
This follows the [official delete endpoint](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/delete),
which requires running execution to end before deletion; physical cleanup can continue afterward.

Each native fixture reserved **$0.03575** through the merged guard with 50k input /
5k output accounting limits, small hosted size, SDK retries disabled and no new
turn input. These are accounting limits; the port has no native output-token cap.
Actual provider tokens and billing are unknown, including hosted compute, so native
reservations remain fully held. PENDING entries made no provider request and settled
at known zero. N11 cumulative conservative hold, including all earlier smoke/probe/
cleanup/failed-attempt receipts: **$3.75025**, below the $15 lane ceiling
and OpenAI's $40 stop on its $50 line. The authoritative ledger is
`lanes/N9-qa/spend.md` in the sprint directory.

## Offline validation

Run `uv run --all-packages --all-extras pytest packages/mux/tests/drivers/openai/test_live_recordings.py -q`
with no provider credential or network required. All 18 tapes must replay under the
independent metadata oracles; extra writes, changed resource identity, unexpected
request fields and non-redacted authorization are rejected. The receipt matrix
must preserve typed gaps, failed attempts, successful driver cleanup and unknown cost.
The ordinary actual-SDK synthetic suite remains separate native-codec evidence.

The original certificate's direct replay command above returned **exit 0, 24
passed**. A docs-only revalidation on post-812 source
`0bfa591a7ee680f4e5e97d9aad72f204c48edbde`, 2026-10-11, also returned **exit 0,
24 passed**, using the retained virtualenv interpreter with
`python -m pytest packages/mux/tests/drivers/openai/test_live_recordings.py -q -n 2`.
These are offline command exits, not fresh live-run outcomes; the prior failed
attempts, historical receipts and unknown billing above remain unchanged.
