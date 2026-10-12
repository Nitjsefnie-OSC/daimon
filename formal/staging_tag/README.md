# Staging tag publication

This model checks two deploy attempts for the same short commit tag. They can
expect the same digest (rerun) or different digests (rebuilt candidate). Each
runner looks up the tag, creates it if missing, then verifies the result. A
second runner may create the tag between lookup and creation. The initial tag
can also be absent or already point to either digest.

| Model action | Workflow and CLI boundary |
| --- | --- |
| `Lookup` | Final `deploy.yml` promotion step reads `gcloud artifacts tags list --filter=... --format=value(version)`. An equal digest succeeds; a different one fails. |
| `Create` | `gcloud artifacts tags create` creates a missing tag. Artifact Registry's `tags.create` is one POST request, modeled as an atomic create that refuses an existing tag. |
| `ResolveCreateRace` | If create loses the race, the workflow reads the tag again and accepts only the expected digest. The unsafe variant instead replaces the tag, as `docker tags add` can do. |
| `Verify` | After successful create, the workflow reads back the digest before succeeding. |

The installed gcloud SDK's `lib/surface/artifacts/tags/create.yaml` describes
`tags create` as failing if the tag exists. Its generated Artifact Registry v1
client maps Create to `POST .../tags` with method ID
`artifactregistry.projects.locations.repositories.packages.tags.create`.
By contrast, `lib/googlecloudsdk/command_lib/artifacts/docker_util.py`
`AddDockerTag` calls `DeleteTag` followed by `CreateDockerTag` when the tag
exists. The installed CLI help and source support the create-only assumption;
this model does not prove the remote service implementation.

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `StagingTagSafe` | clean | 20 | Different candidates cannot replace a tag. |
| `StagingTagSame` | clean | 20 | An identical rerun can accept the existing tag. |
| `StagingTagUnsafe` | violates `ImmutableTag` | 20 | Both runners see a missing tag; one creates digest A, then the other replaces it with B. |

`ImmutableTag` requires at most one digest in the tag's history.
`AcceptedDigestMatches` requires each successful runner's expected digest to
still be present. The model treats each registry create/read as one atomic
action and does not model API outages, IAM, or registry consistency. A failed
lookup is covered by the fake CLI test, which requires failure without a
create call. `tests/test_staging_tag_promotion.py` also replays the model's
same-digest and different-digest creation races against the actual workflow
script. The first natural main CI run remains the deployment check.

Run these small TLC configs sequentially from this directory, using the
repository's pinned `TLA2TOOLS_JAR`:

```sh
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config StagingTagSafe.cfg StagingTag.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config StagingTagSame.cfg StagingTag.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config StagingTagUnsafe.cfg StagingTag.tla
```

The unsafe command exits 12 after the expected invariant violation.
