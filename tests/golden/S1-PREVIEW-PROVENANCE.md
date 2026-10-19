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
