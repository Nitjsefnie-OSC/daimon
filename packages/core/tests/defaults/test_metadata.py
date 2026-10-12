from __future__ import annotations

import re
import uuid

from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    build_metadata,
    find_conflicting_skill_body,
    find_mount_collision,
    skill_owner_candidates,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)


def test_build_metadata_round_trip() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    md = build_metadata(tenant_id=tenant_id, name="daimon")
    assert md[MA_METADATA_KEY_TENANT] == str(tenant_id)
    assert md[MA_METADATA_KEY_NAME] == "daimon"
    assert len(md) == 2


def test_build_metadata_omits_account_when_none() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    md = build_metadata(tenant_id=tenant_id, name="daimon", account_id=None)
    assert MA_METADATA_KEY_ACCOUNT not in md, (
        "account_id=None must not stamp daimon_account — preserves seeded-default "
        "'everyone's agent' semantics"
    )
    assert set(md.keys()) == {MA_METADATA_KEY_TENANT, MA_METADATA_KEY_NAME}


def test_build_metadata_stamps_account_when_provided() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    account_id = uuid.uuid4()
    md = build_metadata(tenant_id=tenant_id, name="user-agent", account_id=account_id)
    assert md[MA_METADATA_KEY_ACCOUNT] == str(account_id), (
        "account_id=UUID must stamp daimon_account=str(UUID) for per-user roster filter"
    )
    assert md[MA_METADATA_KEY_TENANT] == str(tenant_id)
    assert md[MA_METADATA_KEY_NAME] == "user-agent"
    assert len(md) == 3


def test_build_metadata_omits_isolated_when_false() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    md = build_metadata(tenant_id=tenant_id, name="daimon")
    assert MA_METADATA_KEY_ISOLATED not in md, (
        "isolated defaults to False and must not stamp daimon_isolated at all"
    )


def test_build_metadata_stamps_isolated_when_true() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    md = build_metadata(tenant_id=tenant_id, name="reader", isolated=True)
    assert md[MA_METADATA_KEY_ISOLATED] == "true", (
        "isolated=True must stamp daimon_isolated='true' (string, MA metadata values are strings)"
    )


# ---------------------------------------------------------------------------
# tenant_scoped_display_title tests
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("0b8b5903-1234-5678-9abc-def012345678")
_T8 = _TENANT_ID.hex  # the full id, 32 hex chars


def test_tenant_scoped_display_title_seeded_shape_short_name() -> None:
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth")
    assert result == f"{_T8}-cli-auth", (
        "seeded shape (agent_name=None) must return prefix+name verbatim when within limit"
    )


def test_tenant_scoped_display_title_seeded_shape_produces_prefix_dash_name() -> None:
    """seeded shape must produce f'{tenant_id.hex}-{name}' for short names"""
    name = "brainstorming"
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=name)
    assert result == f"{_T8}-{name}", (
        "seeded shape for a short name must be exactly prefix + '-' + name"
    )


def test_tenant_scoped_display_title_synced_shape() -> None:
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth", agent_name="daimon")
    assert result == f"{_T8}-daimon/cli-auth", (
        "synced shape (agent_name provided) must return prefix+agent_name/name"
    )


def test_tenant_scoped_display_title_boundary_63_chars_unmangled() -> None:
    # prefix is 33 chars (32 hex + "-"), so body must be 30 to reach total 63
    body = "x" * 30
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(result) == 63, "63-char title must not be mangled"
    assert result == f"{_T8}-{body}", "63-char title must be verbatim prefix+body"


def test_tenant_scoped_display_title_boundary_64_chars_unmangled() -> None:
    # body must be 31 to reach total 64
    body = "x" * 31
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(result) == 64, "64-char title must not be mangled"
    assert result == f"{_T8}-{body}", "64-char title must be verbatim prefix+body"


def test_tenant_scoped_display_title_boundary_65_chars_mangled_to_64() -> None:
    # body must be 32 to reach total 65 → triggers mangling
    body = "x" * 32
    result = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(result) == 64, "mangled title must be exactly 64 chars"
    assert re.search(r"~[0-9a-f]{4}$", result), "mangled title must end with ~<4 hex chars>"


def test_tenant_scoped_display_title_hash_over_full_body_produces_distinct_titles() -> None:
    """Two long names sharing their first 50 chars must hash to different titles."""
    shared_prefix = "a" * 50
    body_a = shared_prefix + "b" * 10
    body_b = shared_prefix + "c" * 10
    result_a = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body_a)
    result_b = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body_b)
    assert result_a != result_b, (
        "hash is over full body so two long names sharing prefix must produce distinct titles"
    )
    # Both must be exactly 64 chars
    assert len(result_a) == 64
    assert len(result_b) == 64


def test_tenant_scoped_display_title_is_deterministic() -> None:
    kwargs = {"tenant_id": _TENANT_ID, "name": "my-skill", "agent_name": "daimon"}
    assert tenant_scoped_display_title(**kwargs) == tenant_scoped_display_title(**kwargs), (
        "same inputs must always produce identical output"
    )


# ---------------------------------------------------------------------------
# strip_tenant_prefix tests
# ---------------------------------------------------------------------------


def test_strip_tenant_prefix_round_trip_seeded() -> None:
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth")
    body = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert body == "cli-auth", "strip must recover the body from a canonical seeded title"


def test_strip_tenant_prefix_round_trip_synced() -> None:
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name="cli-auth", agent_name="daimon")
    body = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert body == "daimon/cli-auth", "strip must recover the body from a canonical synced title"


def test_strip_tenant_prefix_returns_none_for_other_tenant() -> None:
    other_tenant = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    title = tenant_scoped_display_title(tenant_id=other_tenant, name="cli-auth")
    result = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert result is None, "strip must return None for a title prefixed with a different tenant"


def test_strip_tenant_prefix_returns_none_for_unprefixed_title() -> None:
    result = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title="cli-auth")
    assert result is None, "strip must return None for a bare title without any prefix"


def test_strip_tenant_prefix_mangle_stability() -> None:
    """For a mangled title, strip then re-prefix must reproduce the same <=64-char string."""
    body = "x" * 60  # long enough to trigger mangling
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=body)
    assert len(title) == 64, "title should be mangled"
    stripped = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert stripped is not None, "strip must recognize own tenant's mangled title"
    prefix = f"{_T8}-"
    re_prefixed = f"{prefix}{stripped}"
    assert re_prefixed == title, (
        "re-prefixing a stripped mangled title must reproduce the same 64-char string — no re-mangle"
    )


# Two tenants whose ids share their first 8 hex chars.
_TWIN_A = uuid.UUID("0b8b5903-0000-4000-8000-00000000000a")
_TWIN_B = uuid.UUID("0b8b5903-ffff-4fff-8fff-00000000000b")


def test_title_carries_the_whole_tenant_id() -> None:
    title = tenant_scoped_display_title(tenant_id=_TWIN_A, name="notes")
    assert title == f"{_TWIN_A.hex}-notes", "the prefix is the full id, not its first 8 chars"


def test_tenants_sharing_an_8_hex_prefix_never_own_each_others_skills() -> None:
    for owner, other in ((_TWIN_A, _TWIN_B), (_TWIN_B, _TWIN_A)):
        for kwargs in (
            {"name": "notes"},
            {"name": "notes", "agent_name": "bot"},
            {"name": "x" * 60},
        ):
            title = tenant_scoped_display_title(tenant_id=owner, **kwargs)
            assert strip_tenant_prefix(tenant_id=owner, display_title=title) is not None
            assert strip_tenant_prefix(tenant_id=other, display_title=title) is None, (
                "a tenant sharing the first 8 hex chars must not strip another's title"
            )
    assert tenant_scoped_display_title(tenant_id=_TWIN_A, name="notes") != (
        tenant_scoped_display_title(tenant_id=_TWIN_B, name="notes")
    ), "same name, different tenant: different titles, so lookups cannot cross"


def _scoped_body(agent_name: str, name: str = "notes") -> str:
    title = tenant_scoped_display_title(tenant_id=_TENANT_ID, name=name, agent_name=agent_name)
    body = strip_tenant_prefix(tenant_id=_TENANT_ID, display_title=title)
    assert body is not None
    return body


def test_skill_owner_candidates_prefers_the_stored_owner() -> None:
    owners = skill_owner_candidates(
        "shared/notes", stored_owner="own", agent_names={"shared", "own"}
    )
    assert owners == {"own"}, "a stored owner wins over the title"


def test_skill_owner_candidates_reads_an_unshortened_title() -> None:
    assert skill_owner_candidates(_scoped_body("own"), stored_owner=None, agent_names=set()) == {
        "own"
    }
    assert skill_owner_candidates("notes", stored_owner=None, agent_names={"own"}) == set(), (
        "a tenant skill has no owning agent"
    )


def test_skill_owner_candidates_matches_a_maximum_length_agent_name() -> None:
    long_name = "a" * 64
    body = _scoped_body(long_name)
    assert "/" not in body, "a 64-char agent name pushes the '/' past the kept title"
    owners = skill_owner_candidates(body, stored_owner=None, agent_names={long_name, "other"})
    assert owners == {long_name}, "the shortened title still names its agent"
    rivals = {long_name, "a" * 63}
    assert skill_owner_candidates(body, stored_owner=None, agent_names=rivals) == rivals, (
        "every agent the shortened title could belong to counts as an owner"
    )
    unknown = skill_owner_candidates(
        _scoped_body("b" * 64), stored_owner=None, agent_names={"other"}
    )
    assert unknown == set(), "no known agent fits"


def test_find_conflicting_skill_body_registry_create_hits_agent_scoped_same_name() -> None:
    result = find_conflicting_skill_body(
        existing_bodies=["research daimon/last30days", "cli-auth"],
        name="last30days",
        agent_name=None,
    )
    assert result == "research daimon/last30days", (
        "creating a registry skill must conflict with an agent-scoped skill of the same "
        "mount name — both would mount at the same path on a shared agent"
    )


def test_find_conflicting_skill_body_scoped_create_hits_registry_same_name() -> None:
    result = find_conflicting_skill_body(
        existing_bodies=["last30days"],
        name="last30days",
        agent_name="research daimon",
    )
    assert result == "last30days", (
        "creating an agent-scoped skill must conflict with a registry skill of the same name"
    )


def test_find_conflicting_skill_body_scoped_create_ignores_other_agents_scoped() -> None:
    result = find_conflicting_skill_body(
        existing_bodies=["other agent/last30days"],
        name="last30days",
        agent_name="research daimon",
    )
    assert result is None, (
        "two agents may each own a scoped skill with the same name — they never mount together"
    )


def test_find_conflicting_skill_body_registry_create_ignores_exact_registry_match() -> None:
    result = find_conflicting_skill_body(
        existing_bodies=["last30days"],
        name="last30days",
        agent_name=None,
    )
    assert result is None, (
        "an exact registry-shape match is the update-in-place path, not a conflict"
    )


def test_find_conflicting_skill_body_no_match_returns_none() -> None:
    result = find_conflicting_skill_body(
        existing_bodies=["research daimon/weekly", "cli-auth"],
        name="last30days",
        agent_name=None,
    )
    assert result is None, "unrelated names must not conflict"


def test_find_mount_collision_registry_and_scoped_same_name() -> None:
    result = find_mount_collision(
        skill_ids=["sk_registry", "sk_scoped"],
        body_by_skill_id={
            "sk_registry": "last30days",
            "sk_scoped": "research daimon/last30days",
        },
    )
    assert result is not None, "registry + scoped same-name must collide"
    mount_name, bodies = result
    assert mount_name == "last30days", "collision must name the shared mount"
    assert sorted(bodies) == ["last30days", "research daimon/last30days"], (
        "collision must list both colliding bodies"
    )


def test_find_mount_collision_unique_names_return_none() -> None:
    result = find_mount_collision(
        skill_ids=["sk_a", "sk_b", "sk_builtin"],
        body_by_skill_id={"sk_a": "last30days", "sk_b": "agent/weekly"},
    )
    assert result is None, (
        "unique mounts must not collide; ids missing from the map (built-ins) are skipped"
    )
