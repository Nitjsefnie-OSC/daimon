# Preview unattended handoff request provenance

Compared with preview base `5c14cac06`, the handoff recordings inherited from
S1 head `693ad107e` add exactly four requests. Their source recording commits
are `95c1489f2` (full handoff) and `233a1c7af` (transcript handoff).

| Recording / zero-based effect index | Request | S1 invariant |
| --- | --- | --- |
| `handoff_full.json` / 3 | `GET /v1/sessions/<id:3>` | Bind the unattended checkpoint decider to the old session's own agent, server URLs, and conversation rules channel. |
| `handoff_full.json` / 5 | `GET /v1/sessions/<id:3>` | Refresh and gate the actual session immediately before the single-attempt unattended message send. |
| `handoff_transcript.json` / 4 | `GET /v1/sessions/<id:3>` | Bind the unattended checkpoint decider to the old session's own agent, server URLs, and conversation rules channel. |
| `handoff_transcript.json` / 6 | `GET /v1/sessions/<id:3>` | Refresh and gate the actual session immediately before the single-attempt unattended message send. |

Every pre-existing effect, request, and database fact remains identical under
whole-record comparison. Both MCP recordings retain preview's fixtures and
remain byte-identical to the preview base.

The documented runner in [README.md](README.md) replayed all four affected
scenarios successfully with `--turn-path legacy`: `mcp_start_turn`,
`mcp_continue_turn`, `handoff_full`, and `handoff_transcript`. The merge required
no regeneration, projection changes, or hand edits to recorded JSON.

## Execution-grant follow-up (2026-10-19)

The later execution-provenance fixes intentionally update only
`mcp_continue_turn.json` and `mcp_cancel_turn.json`, using the documented legacy
recording runner. Their source scenarios model sessions opened through agent
chat, so recorded session responses now carry `daimon_agent_chat=true`.
Continue's requests and effects retain their order and content apart from that
fixture stamp. Cancel adds one ownership/surface read inside its mutation fence,
after its pre-fence read: unauthorized nested callers must be refused before
waiting on a human driver's fence, and authorized callers must be checked again
after waiting. The interrupt remains unconditional after authorization.

These recordings supersede the byte-identical MCP statement above for this
follow-up only. No other recording, projection or oracle rule changes.
Both legacy and mux replay against the same two updated recordings.

## Routine sandbox opt-in (2026-10-20)

The per-routine sandbox opt-in adds one keyword to the scheduler's
`headless_runner.run_turn` call: `sandbox_tools`, the routine's
`sandbox_tools_enabled` flag. `scheduler_run.json` and `sealed_channel.json`
were re-recorded with the documented legacy runner
(`--turn-path legacy --regen scheduler_run sealed_channel`); each gains only
`"sandbox_tools": false` in that call's recorded arguments. No request,
effect or database fact changes, and both recordings replay on the legacy and
mux paths.
