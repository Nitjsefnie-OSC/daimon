# Staging IAP SSH registration join

Question: can either VM refresh begin while its prerequisite SSH key creation
and worker-key propagation check is still running or has failed?

| Model action | Implementation boundary |
| --- | --- |
| `Start` | `deploy.yml` starts `scripts/ci/iap-ssh-key.sh run` in the background only for prebuilt staging images. |
| `FinishSuccess` / `FinishFailure` | The script's `register` subcommand runs the notebook SSH key creation, checks worker VM status, and retries worker SSH acceptance six times at 10-second intervals when the workers VM is `RUNNING`. `run` writes the result to a temporary file and atomically renames it to `status`. |
| `JoinSuccess` / `JoinFailure` / `JoinIncomplete` | The `wait` subcommand accepts only a complete numeric status of zero. A nonzero status, malformed final status, or missing status after the bound fails the step. |
| `WaitTick` / `WaitTimeout` | `wait` polls for at most 190 seconds; the registration process has a 180-second timeout. The model scales that time window to two ticks. |
| `RefreshAfterJoin` | The notebook and worker refresh steps occur only after the join step succeeds. |

The two safety properties require that no refresh starts before a successful
join and that a failed join cannot release either refresh gate.

| Config | Expected verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `StagingSSHJoinSafe` | clean | 30 | Registration success, failure, malformed status and missing-status timeout all preserve the refresh gate. |
| `StagingSSHJoinMissingJoin` | violates `NoRefreshBeforeJoin` | 8 | Broken workflow can start a refresh while registration is still running. |

The model assumes a background process can write its status artifact for the
following GitHub Actions step; the workflow already uses this file-and-join
pattern for the notebook refresh. It does not model GitHub runner process
reaping, GCP key propagation, or gcloud's internal timeout behavior. The shell
tests replay the script against a controlled fake gcloud for blocked success,
worker retry, failure, malformed status and missing status. A real staging run
is still needed to validate runner behavior and measure overlap.

| Source value | Model bound |
| --- | --- |
| Registration timeout is 180s and join timeout is 190s (`deploy.yml`) | Bounded wait is represented by two ticks. |
| Worker propagation tries six SSH probes with 10s between failures (`iap-ssh-key.sh`) | The model treats the whole operation as one bounded success/failure result. |
