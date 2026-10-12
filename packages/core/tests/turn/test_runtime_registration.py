"""Optional provider installation uses the runtime registry without default discovery."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn import runtimes
from daimon.core.turn.errors import AdmissionDenied
from mux.errors import ScopeViolation, UnsupportedCapability

from .test_provider_dispatch import PROFILE, REVISION, SCOPE, admission


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(runtimes, "_RUNTIME_FACTORIES", {})
    return runtimes


def selected_request(**changes):
    return SimpleNamespace(
        admission=admission(),
        deps=SimpleNamespace(turn_path="mux", channel_backends=True),
        scope=SCOPE,
        platform="slack",
        tenant_id=REVISION.channel.tenant_id,
        session_account_id=SCOPE.account_id,
        thread_id="thread",
        **changes,
    )


def installed_entry(monkeypatch, install):
    module = ModuleType("offline_provider")
    module.install = install
    monkeypatch.setitem(sys.modules, module.__name__, module)
    entry = EntryPoint(name=PROFILE, value="offline_provider:install", group="daimon.turn_runtimes")
    return entry


def test_default_profile_never_discovers_optional_distributions(registry, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("default turn discovered provider distributions")

    monkeypatch.setattr(registry, "entry_points", forbidden)
    assert not registry.ensure_channel_runtime("anthropic.managed_agents")
    request = selected_request()
    request.admission = replace(request.admission, backend_revision=None)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        registry.build_channel_runtime(request)
    assert registry._RUNTIME_FACTORIES == {}


def test_explicit_registration_takes_precedence_without_package_discovery(registry, monkeypatch):
    runtime = TurnRuntime(lambda config, scope: object(), object(), object())
    calls = []

    def factory(request):
        calls.append(request)
        return runtime

    def forbidden(**kwargs):
        raise AssertionError("registered provider was discovered again")

    registry.register_channel_runtime(PROFILE, factory)
    monkeypatch.setattr(registry, "entry_points", forbidden)
    request = selected_request()
    assert registry.build_channel_runtime(request) is runtime
    assert calls == [request]


def test_selected_package_installs_once_without_constructing_a_runtime(registry, monkeypatch):
    effects = []
    runtime = TurnRuntime(lambda config, scope: object(), object(), object())

    def factory(request):
        effects.append("construct")
        return runtime

    def install():
        effects.append("install")
        registry.register_channel_runtime(PROFILE, factory)

    entry = installed_entry(monkeypatch, install)

    def discover(**kwargs):
        assert kwargs == {"group": "daimon.turn_runtimes", "name": PROFILE}
        effects.append("discover")
        return EntryPoints((entry,))

    monkeypatch.setattr(registry, "entry_points", discover)
    assert registry.ensure_channel_runtime(PROFILE)
    assert registry.ensure_channel_runtime(PROFILE)
    assert effects == ["discover", "install"]
    assert registry.build_channel_runtime(selected_request()) is runtime
    assert effects == ["discover", "install", "construct"]


@pytest.mark.parametrize("kind", ["absent", "ambiguous", "uninstalled", "invalid", "wrong_profile"])
def test_unavailable_or_ambiguous_installation_refuses_without_a_provider_call(
    registry,
    monkeypatch,
    kind,
):
    effects = []

    def install():
        effects.append("install")
        registry.register_channel_runtime("gemini.inline_reuse", lambda request: None)

    entry = installed_entry(monkeypatch, install)
    if kind == "absent":
        entries = EntryPoints(())
    elif kind == "ambiguous":
        entries = EntryPoints((entry, entry))
    elif kind == "uninstalled":
        entries = EntryPoints(
            (EntryPoint(name=PROFILE, value="absent_provider:install", group=entry.group),)
        )
    elif kind == "invalid":
        sys.modules["offline_provider"].install = object()
        entries = EntryPoints((entry,))
    else:
        entries = EntryPoints((entry,))
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: entries)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        registry.build_channel_runtime(selected_request())
    assert effects == (["install"] if kind == "wrong_profile" else [])


@pytest.mark.parametrize(
    "invalid", ["legacy", "disabled", "foreign_scope", "foreign_provider", "missing_model"]
)
def test_invalid_turn_refuses_before_package_discovery(registry, monkeypatch, invalid):
    def forbidden(**kwargs):
        raise AssertionError("invalid request reached provider installation")

    request = selected_request()
    if invalid == "legacy":
        request.deps.turn_path = "legacy"
    elif invalid == "disabled":
        request.deps.channel_backends = False
    elif invalid == "foreign_scope":
        request.scope = SCOPE.model_copy(update={"tenant_id": "foreign"})
    elif invalid == "foreign_provider":
        request.admission = replace(
            request.admission, backend_revision=REVISION.model_copy(update={"backend": "gemini"})
        )
    else:
        request.admission = replace(
            request.admission, backend_revision=REVISION.model_copy(update={"model": None})
        )
    monkeypatch.setattr(registry, "entry_points", forbidden)
    with pytest.raises((AdmissionDenied, ScopeViolation)):
        registry.build_channel_runtime(request)


def test_duplicate_registration_cannot_replace_the_operator_grant_reader(registry):
    def reader(tenant):
        return ()

    def factory(request):
        return None

    registry.register_channel_runtime(PROFILE, factory, authorizations=reader)
    with pytest.raises(ValueError):
        registry.register_channel_runtime(PROFILE, factory, authorizations=lambda tenant: ())
    assert registry._RUNTIME_FACTORIES[PROFILE].authorizations is reader


def test_resource_authorization_rereads_the_provider_validated_manifest_each_time(
    registry, monkeypatch
):
    grants = SimpleNamespace(
        account_id="caller",
        platform="slack",
        channel_id="channel",
        config_digest="digest",
        project="project",
    )
    calls = []

    def read(tenant):
        calls.append(tenant)
        return (grants,) if len(calls) == 1 else ()

    def forbidden(request):
        raise AssertionError("resource authorization constructed a turn runtime")

    registry.register_channel_runtime(PROFILE, forbidden, authorizations=read)
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == (grants,)
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == ()
    assert calls == ["tenant", "tenant"]


def test_missing_resource_authorizer_refuses_without_runtime_or_legacy_fallback(registry):
    def forbidden(request):
        raise AssertionError("missing authorizer constructed a runtime")

    registry.register_channel_runtime(PROFILE, forbidden)
    with pytest.raises(UnsupportedCapability):
        registry.authorized_channel_deployments(PROFILE, "tenant")


_NATIVE_MODULES = frozenset(
    {
        "daimon.core.turn.openai_host",
        "daimon.core.turn.openai_codec",
        "daimon.core.turn.openai_io",
        "daimon.core.turn.openai_state",
        "daimon.core.turn.openai_deployment",
        "daimon.core.turn.openai_credential_requests",
        "daimon.core.turn.gemini",
        "daimon.core.turn.gemini_prepare",
        "daimon.core.turn.gemini_state",
        "daimon.core.turn.gemini_deployment",
        "daimon.core.turn.gemini_credential_requests",
    }
)


def test_neutral_core_has_no_provider_only_import_edges():
    from daimon.core import channel_backend, mux_backend
    from daimon.core.turn import io, prepare

    modules = (channel_backend, mux_backend, io, prepare, runtimes)
    for module in modules:
        source = Path(module.__file__).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                targets = [imported, *(imported + "." + item.name for item in node.names)]
            elif isinstance(node, ast.Import):
                targets = [item.name for item in node.names]
            else:
                continue
            assert not any(target in _NATIVE_MODULES for target in targets), (
                module.__name__,
                node.lineno,
                targets,
            )


def test_cold_default_and_selected_refusal_work_without_provider_only_modules():
    code = rf"""
import importlib.abc
import json
import sys
from importlib.metadata import EntryPoints
from types import SimpleNamespace

blocked = {set(_NATIVE_MODULES)!r}
class NoNativeModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoNativeModules())
from daimon.core import channel_backend, mux_backend
from daimon.core.turn import io, prepare, runtimes
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, Scope
calls = []
def discover(**kwargs):
    calls.append(kwargs)
    return EntryPoints(())
runtimes.entry_points = discover
assert not runtimes.ensure_channel_runtime("anthropic.managed_agents")
assert calls == []
assert set(io._TURN_CODECS) == {{"anthropic.managed_agents"}}
assert set(mux_backend._TURN_BACKENDS) == {{"anthropic.managed_agents"}}
scope = Scope(tenant_id="tenant", account_id="account", principal_id="caller", authorization_id="authorized")
revision = ConfigRevision.create(ChannelRef(tenant_id="tenant", platform="slack", channel_id="channel"), 1,
    resolve_default(BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna")))
request = SimpleNamespace(admission=SimpleNamespace(backend_revision=revision),
    deps=SimpleNamespace(turn_path="mux", channel_backends=True), scope=scope,
    platform="slack", tenant_id="tenant", session_account_id="account", thread_id="thread")
try:
    runtimes.build_channel_runtime(request)
except AdmissionDenied as error:
    assert error.reason == "backend_unsupported"
else:
    raise AssertionError("missing provider ran or fell back")
assert len(calls) == 1
assert not blocked.intersection(sys.modules)
print(json.dumps({{"default_discovery": 0, "selected_refused": True, "native_modules": 0}}))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_"))
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "default_discovery": 0,
        "selected_refused": True,
        "native_modules": 0,
    }


def test_default_cli_copy_runs_without_provider_only_modules():
    code = rf"""
import asyncio
import importlib.abc
import json
import sys
import uuid
from types import SimpleNamespace
blocked = {set(_NATIVE_MODULES)!r}
class NoNativeModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoNativeModules())
from daimon.adapters.cli.commands import channels
from daimon.core.turn import runtimes
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef

def forbidden(**kwargs):
    raise AssertionError("default CLI discovered an optional provider")
runtimes.entry_points = forbidden
class Session:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
class Factory:
    def __call__(self, **kwargs):
        raise AssertionError("default CLI constructed a resource host")
tenant = uuid.UUID(int=4004)
channel = ChannelRef(tenant_id=str(tenant), platform="slack", channel_id="channel")
revision = ConfigRevision.create(channel, 1, resolve_default(BackendConfig()))
rt = SimpleNamespace(gemini_resource_hosts=None, resource_hosts=Factory(), sessionmaker=Session)
async def prove():
    queries = []
    for configured in (None, revision):
        async def read(session, ref):
            assert ref == channel
            queries.append(ref)
            return configured
        channels.current_backend = read
        result = await channels._copy_resource_host(rt, tenant_id=tenant, platform="slack", channel="channel")
        assert result is None
    assert len(queries) == 2
    assert not blocked.intersection(sys.modules)
    return {{"default_cli_cases": 2, "native_modules": 0}}
print(json.dumps(asyncio.run(prove())))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_"))
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"default_cli_cases": 2, "native_modules": 0}


@pytest.mark.parametrize("profile", [PROFILE, "gemini.inline_reuse"])
def test_installed_provider_registers_all_host_edges_without_credentials_or_clients(profile):
    code = rf"""
import json
import os
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from google.genai import Client

def no_client(*args, **kwargs):
    raise AssertionError("registration constructed a provider client")
for client_type in (AsyncAnthropic, AsyncOpenAI, Client):
    client_type.__init__ = no_client
from daimon.core import mux_backend
from daimon.core.turn import io, prepare, runtimes
profile = {profile!r}
original_get = os.environ.get
secret_names = {{"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"}}
def no_secret(name, *args):
    if name in secret_names:
        raise AssertionError("registration read a credential")
    return original_get(name, *args)
os.environ.get = no_secret
assert runtimes.ensure_channel_runtime(profile)
assert profile in mux_backend._TURN_BACKENDS
assert profile in io._TURN_CODECS
assert profile in prepare._PROVIDER_PREPARATIONS
assert profile in runtimes._RUNTIME_FACTORIES
other = "gemini.inline_reuse" if profile == "openai.persistent_workspace" else "openai.persistent_workspace"
assert other not in mux_backend._TURN_BACKENDS
assert other not in io._TURN_CODECS
assert other not in prepare._PROVIDER_PREPARATIONS
assert runtimes.ensure_channel_runtime(profile)
print(json.dumps({{"selected_registered": True, "other_registered": False}}))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_"))
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"selected_registered": True, "other_registered": False}


def test_missing_host_edge_installs_without_replacing_explicit_constructor_or_reader(
    registry, monkeypatch
):
    runtime = TurnRuntime(lambda config, scope: object(), object(), object())
    effects = []
    host = {"ready": False}

    def explicit(request):
        effects.append("explicit_constructor")
        return runtime

    def reader(tenant):
        effects.append(("explicit_reader", tenant))
        return ()

    def forbidden(*args):
        raise AssertionError("provider default replaced an explicit application callback")

    registry.register_channel_runtime(PROFILE, explicit, authorizations=reader)

    def install():
        effects.append("install")
        host["ready"] = True
        registry.register_channel_runtime(
            PROFILE, forbidden, authorizations=forbidden, fallback=True
        )

    entry = installed_entry(monkeypatch, install)
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: EntryPoints((entry,)))
    assert registry.ensure_channel_runtime(PROFILE, ready=lambda: host["ready"])
    assert effects == ["install"]
    assert registry._RUNTIME_FACTORIES[PROFILE].factory is explicit
    assert registry._RUNTIME_FACTORIES[PROFILE].authorizations is reader
    assert registry.ensure_channel_runtime(PROFILE, ready=lambda: host["ready"])
    assert effects == ["install"], "completed host installation must not rediscover the package"
    assert registry.build_channel_runtime(selected_request()) is runtime
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == ()
    assert effects == ["install", "explicit_constructor", ("explicit_reader", "tenant")]


def test_constructor_without_requested_edge_is_not_a_complete_installation(registry, monkeypatch):
    def forbidden(*args):
        raise AssertionError("incomplete installation constructed a runtime")

    registry.register_channel_runtime(PROFILE, forbidden)
    entry = installed_entry(
        monkeypatch, lambda: registry.register_channel_runtime(PROFILE, forbidden, fallback=True)
    )
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: EntryPoints((entry,)))
    assert not registry.ensure_channel_runtime(PROFILE, ready=lambda: False)
    assert registry._RUNTIME_FACTORIES[PROFILE].factory is forbidden
    assert registry._RUNTIME_FACTORIES[PROFILE].authorizations is None


def test_absent_provider_cannot_complete_an_explicit_runtime_host_edge(registry, monkeypatch):
    def forbidden(*args):
        raise AssertionError("missing package constructed a runtime")

    registry.register_channel_runtime(PROFILE, forbidden)
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: EntryPoints(()))
    assert not registry.ensure_channel_runtime(PROFILE, ready=lambda: False)
    assert registry._RUNTIME_FACTORIES[PROFILE].factory is forbidden


@pytest.mark.parametrize("kind", ["backend", "codec", "preparation"])
def test_provider_fallback_preserves_explicit_host_hooks_and_rejects_application_duplicates(
    monkeypatch, kind
):
    from daimon.core import mux_backend
    from daimon.core.turn import io, prepare

    owner, name, register = {
        "backend": (mux_backend, "_TURN_BACKENDS", mux_backend.register_turn_backend),
        "codec": (io, "_TURN_CODECS", io.register_turn_codec),
        "preparation": (prepare, "_PROVIDER_PREPARATIONS", prepare.register_turn_preparation),
    }[kind]
    monkeypatch.setattr(owner, name, {})

    def explicit(*args):
        raise AssertionError("registration invoked the explicit host hook")

    def fallback(*args):
        raise AssertionError("registration invoked the provider fallback")

    register(PROFILE, explicit)
    register(PROFILE, fallback, fallback=True)
    assert getattr(owner, name)[PROFILE] is explicit
    with pytest.raises(ValueError, match="already registered"):
        register(PROFILE, fallback)
    assert getattr(owner, name)[PROFILE] is explicit


@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("profile", [PROFILE, "gemini.inline_reuse"])
def test_cold_explicit_runtime_reaches_actual_native_preparation(profile, optimized):
    code = rf"""
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from google.genai import Client

def no_client(*args, **kwargs):
    raise RuntimeError("cold registration constructed a native client")
for client_type in (AsyncAnthropic, AsyncOpenAI, Client):
    client_type.__init__ = no_client
from daimon.core import mux_backend
from daimon.core.turn import io, prepare, runtimes
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
sys.path.insert(0, os.path.join(os.getcwd(), "packages/core/tests"))
from turn.test_provider_dispatch import ACCOUNT, TENANT, admission, deps_for
profile = {profile!r}
if profile in prepare._PROVIDER_PREPARATIONS:
    raise RuntimeError("test did not start with a cold native preparation edge")
class FactoryReached(Exception):
    pass
calls = []
reads = []
install_results = []
install = prepare.ensure_channel_runtime
def observed_install(*args, **kwargs):
    result = install(*args, **kwargs)
    install_results.append(result)
    return result
prepare.ensure_channel_runtime = observed_install
def factory(request):
    calls.append(request.admission.backend_revision.profile)
    raise FactoryReached()
def authorizations(tenant):
    reads.append(tenant)
    return ()
runtimes.register_channel_runtime(profile, factory, authorizations=authorizations)
original_get = os.environ.get
def no_secret(name, *args):
    if name in {{"DAIMON_TURN__PROVIDER_RUNTIME_FILE", "DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"}}:
        raise RuntimeError("cold installation inspected deployment or credentials")
    return original_get(name, *args)
os.environ.get = no_secret
async def prove():
    selected = admission()
    config = ConfigRevision.create(selected.backend_revision.channel, 1,
        resolve_default(BackendConfig(backend="openai" if profile == "openai.persistent_workspace" else "gemini",
            profile=profile, model="gpt-6-luna" if profile == "openai.persistent_workspace" else "gemini-3.8-flash")))
    selected = replace(selected, backend_revision=config, backend_revision_origin="channel")
    deps = replace(deps_for(None, async_sessionmaker()), channel_backends=True,
        turn_runtime_factory=runtimes.build_channel_runtime)
    try:
        await prepare.bind_session_impl(deps, selected, tenant_id=TENANT, platform="slack",
            external_user_id="caller", thread_id="thread", session_account_id=ACCOUNT,
            reuse_existing=True, deadline=datetime.now(UTC)+timedelta(seconds=15))
    except FactoryReached:
        pass
    else:
        raise RuntimeError("actual native preparer did not reach the explicit constructor")
    if calls != [profile] or install_results != [True]:
        raise RuntimeError("explicit constructor or completed installation differs")
    if any(profile not in owner for owner in (mux_backend._TURN_BACKENDS, io._TURN_CODECS, prepare._PROVIDER_PREPARATIONS)):
        raise RuntimeError("provider host bundle remained incomplete")
    record = runtimes._RUNTIME_FACTORIES[profile]
    if record.factory is not factory or record.authorizations is not authorizations:
        raise RuntimeError("provider registrar replaced an explicit callback")
    if runtimes.authorized_channel_deployments(profile, "tenant") != () or reads != ["tenant"]:
        raise RuntimeError("explicit resource authorization was not retained")
    return {{"constructor_calls": len(calls), "host_bundle_complete": True, "explicit_reader_retained": True}}
print(json.dumps(asyncio.run(prove())))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_"))
    }
    command = [sys.executable, *(["-O"] if optimized else []), "-c", code]
    result = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=40, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "constructor_calls": 1,
        "host_bundle_complete": True,
        "explicit_reader_retained": True,
    }
