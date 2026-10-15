"""Explicit full-byte skill deployment through documented GCS directory sources.

The host supplies an authorized publisher; the driver never discovers storage
credentials, makes a bucket public, or silently chooses a publisher. Its receipt
attests an immutable directory containing exactly the supplied file manifest.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit

from mux.contracts.ids import Scope
from mux.contracts.resources import SkillUpload
from mux.errors import ProviderError, ScopeViolation


def file_manifest(bundle: SkillUpload) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((file.path, sha256(file.content).hexdigest()) for file in bundle.files))


@dataclass(frozen=True)
class GCSBundleDeployment:
    tenant_id: str
    account_id: str
    account_scope_id: str
    version: str
    source: str
    files: tuple[tuple[str, str], ...]


class SkillBundlePublisher(Protocol):
    async def publish(
        self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
    ) -> GCSBundleDeployment:
        """Publish every original byte; idempotent on owner/account/version.

        Keep the versioned directory immutable and readable by the environment
        for the lifetime of its pins. Include no extra files or credentials in
        the source URI. Failure must not acknowledge a successful deployment.
        Bucket access/authorization and retention are supplied by the host.
        """
        ...


class RetainedSkillDeployments:
    """Use explicitly provisioned immutable directories; never upload or discover credentials.

    The provisioning owner must supply independently read-back receipts. This
    adapter lets the existing skill owner consume them with the same complete
    scope/version/file checks as a publisher that performed the upload itself.
    """

    def __init__(self, deployments: tuple[GCSBundleDeployment, ...]) -> None:
        self._deployments = {
            (item.tenant_id, item.account_id, item.account_scope_id, item.version): item
            for item in deployments
        }
        if len(self._deployments) != len(deployments):
            raise ValueError("duplicate skill deployment receipt")

    async def publish(
        self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
    ) -> GCSBundleDeployment:
        deployment = self._deployments.get(
            (scope.tenant_id, scope.account_id, account_scope_id, version)
        )
        if deployment is None:
            raise ScopeViolation(version, "no authorized retained skill deployment")
        validate_deployment(deployment, scope, account_scope_id, version, bundle)
        return deployment


def validate_deployment(
    deployment: GCSBundleDeployment, scope: Scope, account: str, version: str, bundle: SkillUpload
) -> None:
    if (deployment.tenant_id, deployment.account_id, deployment.account_scope_id) != (
        scope.tenant_id,
        scope.account_id,
        account,
    ):
        raise ScopeViolation(version, "skill deployment is outside the authorized scope")
    if deployment.version != version or deployment.files != file_manifest(bundle):
        raise ProviderError("conflict", retryable=False, native_code="skill_deployment_mismatch")
    uri = urlsplit(deployment.source)
    # Versioned directory, not a mutable latest alias, URL token or file mount.
    if (
        uri.scheme != "gs"
        or not uri.netloc
        or uri.username is not None
        or ":" in uri.netloc
        or uri.query
        or uri.fragment
        or not uri.path.endswith(f"/{version}/")
        or any(part in (".", "..") for part in uri.path.split("/"))
        or any(char.isspace() or char in "%\\\x00" for char in deployment.source)
    ):
        raise ValueError("skill deployment needs a credential-free versioned GCS directory")
