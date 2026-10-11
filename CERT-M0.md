# M0 / A7 gate certification — 2026-10-11

**A7 evidence gate satisfied: replay overhead within the 5/20 ms gate,
paired live Anthropic probe PASS, and the turn path still defaults to legacy.**
This certificate records the lead-selected A7 rubric evidence. It makes no claim
of a quiet-host live latency certificate or the full 33-recipe live set.

The live source is merged integration
`570f7ca1d6615a46d0b55ddea0b0f37db65b55e9` (#827).
All `lanes/` and `inbox/` evidence paths below are relative to the external sprint
archive `/home/clsandoval/cs/daimon-neutral-core-20261009`, not this repository.
Their SHA256 hashes pin the preserved bytes; private evidence remains in that
archive and is not copied into Git. This commit changes documentation only.

## Replay performance gate

The certifying artifact is
`lanes/N4-turn/U2-TURN-BRIDGE-REPLAY-20261009T192322Z.json`.
Its recorded measurement timestamp is `2026-10-09T19:13:29.203690+00:00`,
with 200 iterations after 20 warmups. It reports `gate_passed: true`,
**p50 added 0.682375 ms / p95 added 6.294369 ms** against the
**p50 <= 5 ms / p95 <= 20 ms** replay-overhead limits.
The effect digest is
`e99dd0d8dc65890ecff2fa41f73866739db498dce0308813b4a635cd69da31e0`.
This is the historical replay measurement selected for the gate, not a new
benchmark of the October 11 source or an inference from live wall times.

Loaded-host reviewer reruns are retained, not relabelled PASS:
`U2-TURN-BRIDGE-REPLAY-REVIEW-20261009T194459Z.json` reports
+1.643047 / +29.119637 ms; the `REVIEW-REPEAT` artifact reports
+1.292238 / +24.286676 ms. Both have `gate_passed: false` because their
p95 exceeds 20 ms. They are **not the certifying run** and do not establish
an unconditional performance guarantee under load.

## Authorized paired live probe

A7-R3 ran exactly once with `claude-haiku-5-5`, from the merged source above,
under independently published lead GO
`e0ad5784b4ffc8e86f67ac3ef744c244386f1b6867704e080f6260f4e83d99bb`.
This is the canonical sorted, compact GO JSON digest; the file-byte hash of the
pretty-printed GO draft is separately listed below. Financial authorization was
`5dc7a801d7566e63e91fd9e30f2c667db613166ab27eedd1d629af616950b9a1`.
Run ID: **`b2565a4732e34f3c8b0d5cde5e13b997`**.

The audited live receipt records `result.status: pass`. Both the legacy and mux
legs completed and independently persisted the same exact final text,
`a7-paired-probe-ok`, before subsequent guards/cleanup. The legacy leg took
1699.922 ms and the mux leg 1734.876 ms; these are individual request wall times,
not a latency distribution. The mux leg persisted nine journal records.
Two sessions were created and both archived. No orphan, pending acknowledgement,
pending response or open response remained. Disposable *-r3 agent/environment
archive ACKs were independently verified; both sessions read back as archived
and terminated in the cleanup receipt. SDK/HTTP retries were zero.

Each leg reported 646 inclusive input tokens and 13 output tokens. Legacy
reported 642 aggregate cache-write tokens without a TTL split; mux reported
642 cache-read tokens. The output comparison is computable and equal; the
usage categories and underlying charges are not asserted equal.
**Actual cost remains unknown (`actual_usd: null`); the $0.35 hold is retained**
with `estimated_unverified` / `unknown_usage` accounting pending N8 reconciliation.
The stream estimate $0.000785387231099473 is not settled actual spend.
No hold is released and no second settlement is asserted by this certificate.

Two preceding attempts remain FAILED:

- Original A7, run `5506581b762241c8a1177a4202c1d041`: stopped in the first
  leg when the monitor refused unresolved cache-write TTL pricing. Export then
  refused trusted outbound request-path strings as ambiguous encoded content.
  The normalized meter supports the missing TTL diagnosis; the original raw SSE
  frame was not retained. The pair was incomplete and never a parity PASS.
- A7-R2, run `07f71f5a275b47f4a42338bec3d352b2`: both model requests completed,
  but the harness rejected the mux's successful `processed` send claim because
  it accepted only `accepted`. The later second mux POST was the failure-cleanup
  interrupt, not another user message. Legacy final bytes were not retained, so
  that failed attempt cannot supply a saved-output parity certificate.

Both prior attempts were cleaned up, and each retains its own $0.35 unknown-cost
hold. These are three separately authorized attempts, not retries of one session.
The failed receipts and diagnosis notes remain unchanged and hashed below.

## Default legacy proof

At the certified source, [TurnSettings](packages/core/daimon/core/config.py)
sets `path: Literal["legacy", "mux"]` to `default="legacy"`, maps an empty
path to legacy, and leaves `channel_backends=False`. Explicit opt-in is required.

[The existing settings regression](packages/core/tests/turn/test_path_settings.py),
`test_turn_flag_matches_main_settings_and_turn_only_loader`, checks unset,
empty, explicit legacy and explicit mux values through both settings loaders,
and asserts `TurnSettings().path == "legacy"`. The file also tests refusal of
unknown values, the credential-independent turn loader and explicit override
precedence. The focused file was rerun on the certified source for this document:
`pytest packages/core/tests/turn/test_path_settings.py -n 2` — **8 passed**
(3.21 s), with provider credentials unset and the owned N4 test DB selected.
No configuration or product code is changed by the certificate.

## Limits

This certificate does **not** certify quiet-host live p50/p95 latency, the
33-recipe live Anthropic set, exact paid cost/runtime, all-provider conformance,
or F2 skills/builtins/MCP execution. The live receipt explicitly has
`quiet_host_latency_certificate: false` and `full_33_recipe_certificate: false`.
The replay gate and the single paired live probe are separate evidence.

## Evidence SHA256 manifest

| Preserved path | SHA256 |
| --- | --- |
| `lanes/N4-turn/U2-TURN-BRIDGE-REPLAY-20261009T192322Z.json` | `9bb37a4d507b7fd2f5e4ff546f668e12c8eb34e745f1e120d400f20f5ff0c75b` |
| `lanes/N4-turn/U2-TURN-BRIDGE-REPLAY-REVIEW-20261009T194459Z.json` | `5756642238aedb9a12811944e23a9e3fcd11e94bc362fb4fc0298b36c60053f8` |
| `lanes/N4-turn/U2-TURN-BRIDGE-REPLAY-REVIEW-REPEAT-20261009T194459Z.json` | `791200c6e4a1d8eecec75bef1d377501722c312801401f2a9ae263a4219e778c` |
| `lanes/N4-turn/a7-live-r3-prepared/result/receipt.json` | `1bb171c9203a2b1dc00e3e3263cfe4357606961ae8045181850cabdaa24b8a0e` |
| `lanes/N4-turn/a7-live-r3-prepared/result/a7-legacy-leg.json` | `e9e6b5728302f2b30672e7bd6f6e268857805593d7ba5beceb64395ad0316432` |
| `lanes/N4-turn/a7-live-r3-prepared/result/a7-mux-leg.json` | `24de3003e9b23f360b04006f50f3e607e3af3220aaa20f38565843d0d39024df` |
| `lanes/N4-turn/a7-live-r3-prepared/A7-R3.go-draft.json` | `c286bf2e1cbe3c8296956e946909db6e6e5a2cc316496e05efe0781f7ff6b7e8` |
| `lanes/N4-turn/a7-live-r3-prepared/cleanup-receipt.json` | `04ac1bdebe832c6d23a0474752b1e8d9137db3476377903e6ab11b537ae13475` |
| `lanes/N4-turn/a7-live-20261011/result/receipt.json` | `536915088ed6311d3ac8513d2786fd582404df20ea2420d324c86b3247762a16` |
| `lanes/N4-turn/a7-live-r2-prepared/result/receipt.json` | `f901e6ca5858e8f10b3d3800d10d4a31146c507a6800db5b3ce18f76de9cec26` |
| `inbox/20261011T151705Z-N4-LEAD-N8-N3-A7-REFUSALS-DIAGNOSIS-PROPOSAL-NOTE.md` | `f7625ac870d1e63472ab5b203c22dff0bcae05a6a8699d072d67c7c1ec22e345` |
| `inbox/20261011T155341Z-N4-LEAD-N8-A7-R2-CLAIM-REFUSAL-DIAGNOSIS-NOTE.md` | `4a18281831f1d51c4905bf7999d8b3c4f5b01371e4856a27b464582904c3dbb7` |
| `inbox/20261011T162636Z-LEAD-A7-R3-GO-PIN.md` | `5ce6dd8594c5c76c47427765d02839d6456c62bc7edefe830692263d11571f65` |
| `packages/core/daimon/core/config.py` | `c1118dc6fbcf69a7bf9cfb3cca2faf4edd4e1658b78ed496ba36997a3964aa76` |
| `packages/core/tests/turn/test_path_settings.py` | `c454e2becd4b56ec2c61f021fd20884c0100a6838b73fcfa0effbb278f53c9e4` |
