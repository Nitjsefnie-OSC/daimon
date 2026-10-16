"""The repository coverage predicate shared by Python and SQL readers."""

from __future__ import annotations

from daimon.core._models import GitHubAppInstallation
from daimon.core.stores.domain import GitHubAppInstallationRow


def installation_covers_repo(
    installation: GitHubAppInstallation | GitHubAppInstallationRow | None,
    repo_full_name: str,
) -> bool:
    return bool(
        installation is not None
        and installation.suspended_at is None
        and installation.github_removed_at is None
        and repo_full_name in installation.repo_full_names
        and (
            installation.github_repo_full_names is None
            or repo_full_name in installation.github_repo_full_names
        )
    )


def installation_covers_repo_sql(repo: str, installation: str) -> str:
    """SQL equivalent; callers supply trusted SQL identifiers, never user input."""
    return (
        f"{installation}.suspended_at IS NULL "
        f"AND {installation}.github_removed_at IS NULL "
        f"AND {repo} = ANY({installation}.repo_full_names) "
        f"AND ({installation}.github_repo_full_names IS NULL "
        f"OR {repo} = ANY({installation}.github_repo_full_names))"
    )
