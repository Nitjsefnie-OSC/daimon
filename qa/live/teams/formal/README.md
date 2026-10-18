# Post reconciliation model

This finite model checks the two-POST bound and that a found correlation
prevents the second POST. `FirstPost`, `Found`, `Absent`, and `Retry` correspond
to `GraphTeamsDriver.post_mention` and `find_by_correlation` in `graph.py`.
The first POST's ambiguous outcome is deliberately left unspecified.
The model does not establish that Graph search is immediately consistent; a
late-visible first POST can still make an absent search misleading. The
offline tests replay the found and absent branches with `httpx.MockTransport`.
