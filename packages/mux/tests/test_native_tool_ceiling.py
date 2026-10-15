"""Native projection and compilation must preserve the exact permission ceiling."""

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.drivers.anthropic.resources.agents import agent_payload
from mux.drivers.anthropic.schemas import agent_tools


def project(configs, defaults):
    return agent_tools(
        ExtensionConfig(
            namespace="anthropic.agent_tools",
            version=1,
            value={
                "tools": [
                    {
                        "type": "agent_toolset_20260401",
                        "configs": configs,
                        "default_config": defaults,
                    }
                ]
            },
        )
    )


@pytest.mark.parametrize(
    "configs,defaults,expected,missing",
    [
        ([], {}, (), True),
        ([{"name": "read"}], {}, (), True),
        ([{"name": "bash", "enabled": False}], {"enabled": True}, (), True),
        ([{"name": "read"}], {"enabled": False}, (), False),
        ([{"name": "read", "enabled": True}], {"enabled": False}, (), True),
        (
            [{"name": "read", "enabled": True, "permission_policy": {"type": "always_ask"}}],
            {"enabled": False, "permission_policy": {"type": "always_allow"}},
            (("read", "ask"),),
            False,
        ),
        (
            [{"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}}],
            {"enabled": False, "permission_policy": {"type": "always_ask"}},
            (("read", "auto"),),
            False,
        ),
        (
            [{"name": "read", "enabled": True}, {"name": "bash", "enabled": False}],
            {"enabled": False, "permission_policy": {"type": "always_ask"}},
            (("read", "ask"),),
            False,
        ),
        (
            [{"name": "bash", "enabled": False}, {"name": "bash", "enabled": True}],
            {"enabled": False, "permission_policy": {"type": "always_allow"}},
            (),
            False,
        ),
        (
            [
                {"name": "read", "enabled": True, "permission_policy": {"type": "always_ask"}},
                {"name": "read", "enabled": True},
            ],
            {"enabled": False, "permission_policy": {"type": "always_allow"}},
            (("read", "ask"),),
            False,
        ),
    ],
)
def test_projection_requires_complete_controls_and_uses_strictest_duplicates(
    configs, defaults, expected, missing
):
    native = project(configs, defaults)
    assert tuple((t.name, t.permission) for t in native.intent()) == expected
    assert native.has_unrepresented_toolset() is missing


@pytest.mark.parametrize("permission,policy", [("auto", "always_allow"), ("ask", "always_ask")])
def test_read_only_neutral_grant_compiles_to_only_read(permission, policy):
    spec = AgentSpec(
        name="reader",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        tools=(ToolSpec(name="read", kind="builtin", permission=permission),),
    )
    tools = agent_payload(spec)["tools"]
    assert tools == [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            "configs": [{"name": "read", "enabled": True, "permission_policy": {"type": policy}}],
        }
    ]
    assert (
        agent_tools(
            ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={"tools": tools})
        ).intent()
        == spec.tools
    )
