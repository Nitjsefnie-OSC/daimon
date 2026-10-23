"""An agent with a login-bearing connector runs a fleet; its workers never get the login."""

from __future__ import annotations

import pytest
from daimon.core.neutral_resources import (
    WorkerSpecRefused,
    fleet_root_obstacle,
    resolve_worker_spec,
    task_worker_spec,
)
from daimon.core.turn.runtime_manifest import login_mcp_servers, worker_source_ceiling
from mux.contracts.resources import MCPConnection, ResourceBinding, ToolSpec

from ..test_agent_backend import owned as owned
from .test_fleet_source_permissions import NOW, compose
from .test_fleet_source_permissions import captured as captured

LOGIN = "vault:linear-login"


def with_login_connector(inputs, *, paired: bool = True):
    """The built-in Daimon's shape: a connector whose login the host projects."""
    source, *rest = inputs
    spec = source.effective_spec
    source = source.model_copy(
        update={
            "effective_spec": spec.model_copy(
                update={
                    "mcp_servers": (
                        *(spec.mcp_servers or ()),
                        MCPConnection(
                            name="linear", url="https://linear.invalid/mcp", credential_ref=LOGIN
                        ),
                    ),
                    "tools": (
                        *(spec.tools or ()),
                        ToolSpec(name="linear", kind="mcp_toolset"),
                    ),
                }
            ),
            "resource_requirements": (
                *source.resource_requirements,
                ResourceBinding(id=LOGIN, kind="credential", credential_ref=LOGIN)
                if paired
                else ResourceBinding(
                    id="vault:orphan", kind="credential", credential_ref="vault:orphan"
                ),
            ),
        }
    )
    return (source, *rest)


def mentions_login(value: object) -> bool:
    import json

    return LOGIN in json.dumps(value, default=str)


async def test_a_login_connector_is_not_a_fleet_obstacle(captured):
    inputs = with_login_connector(captured)
    source = inputs[0]
    assert login_mcp_servers(source) == {"linear"}
    assert fleet_root_obstacle(source) is None
    ceiling = worker_source_ceiling(source)
    assert "linear" not in {server.name for server in ceiling.effective_spec.mcp_servers or ()}
    assert "linear" not in {tool.name for tool in ceiling.effective_spec.tools or ()}
    assert "mcp" in ceiling.effective_spec.declared_drops
    assert not any(binding.credential_ref == LOGIN for binding in ceiling.resource_requirements)


async def test_the_root_freezes_without_the_connector_and_no_worker_gets_its_login(captured):
    inputs = with_login_connector(captured)
    source, _, grant, _, policy, _ = inputs
    root = compose(inputs)
    assert "linear" not in {server.name for server in root.spec.mcp_servers or ()}
    assert not any(binding.credential_ref == LOGIN for binding in root.resources)
    assert "mcp" in root.spec.declared_drops
    assert not mentions_login(root.spec.model_dump(mode="json"))
    assert not mentions_login([binding.model_dump(mode="json") for binding in root.resources])
    from daimon.core.neutral_resources import default_worker_limits

    for model in (None, *(model.id for model in grant.models)):
        spec = task_worker_spec(root, role="W", task="Compare X and Y.", model=model)
        assert "mcp" in spec.declared_drops
        child = resolve_worker_spec(
            root,
            spec,
            policy=policy,
            now=NOW,
            captured_source=source,
            resources=root.resources if spec.model.provider == "anthropic" else (),
            environment=root.environment if spec.model.provider == "anthropic" else None,
            limits=default_worker_limits(
                root.limits,
                None,
                (),
                provider=spec.model.provider,
                budget_usd=root.limits.budget_usd,
            ),
        )
        assert not mentions_login(child.spec.model_dump(mode="json"))
        assert not mentions_login([binding.model_dump(mode="json") for binding in child.resources])
        assert not any(binding.kind == "credential" for binding in child.resources)


async def test_a_clone_worker_can_not_put_the_connector_back(captured):
    inputs = with_login_connector(captured)
    source, _, _, _, policy, _ = inputs
    root = compose(inputs)
    widened = root.spec.model_copy(
        update={"mcp_servers": source.effective_spec.mcp_servers, "declared_drops": ()}
    )
    with pytest.raises(WorkerSpecRefused):
        resolve_worker_spec(
            root,
            widened,
            policy=policy,
            now=NOW,
            captured_source=source,
            resources=(
                *root.resources,
                ResourceBinding(id=LOGIN, kind="credential", credential_ref=LOGIN),
            ),
            environment=root.environment,
            limits=root.limits,
        )


async def test_a_login_with_no_connector_still_refuses(captured):
    inputs = with_login_connector(captured, paired=False)
    assert fleet_root_obstacle(inputs[0]) == "credentials"
    with pytest.raises(WorkerSpecRefused):
        compose(inputs)
