"""Ratchet Discord readers and persistent controls against audited entry boundaries.

The registry is an explicit review artifact. Adding another primitive, caller,
control class or duplicate call requires an entry with a boundary and regression.
Line numbers are deliberately excluded, so formatting cannot silently bless code.
This is a review ratchet, not exhaustive discovery or a proof of authorization.
Bound-method aliases, getattr/reflection, constructed URL variables and lower-level
SDK HTTP primitives can escape detection. Guard strings and regression names do
not prove reachability, guard order or control flow. Manual reader inventory and
behavioral regressions remain necessary, including stored-content projections.
"""

import ast
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = Path(__file__).with_name("discord_reader_registry.json")
TRUSTED_RECRUITMENT = {
    "packages/adapters/discord/daimon/adapters/discord/recruitment.py",
    "packages/core/daimon/core/recruitment_discord.py",
    "packages/core/daimon/core/recruitment_runtime.py",
}


def discover(root: Path) -> Counter[str]:
    result: Counter[str] = Counter()
    control_names = {"View", "LayoutView", "Modal", "DynamicItem"}

    def base_name(base):
        if isinstance(base, ast.Subscript):
            base = base.value
        return (
            base.attr
            if isinstance(base, ast.Attribute)
            else base.id
            if isinstance(base, ast.Name)
            else ""
        )

    classes = []
    control_aliases = []
    for path in (root / "packages").rglob("*.py"):
        if "/tests/" not in path.as_posix() and "/testing/" not in path.as_posix():
            parsed = ast.parse(path.read_text())
            classes.extend(node for node in ast.walk(parsed) if isinstance(node, ast.ClassDef))
            control_aliases.extend(
                (alias.asname, alias.name)
                for node in ast.walk(parsed)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.asname
            )
    while True:
        found = {alias for alias, original in control_aliases if original in control_names}
        found.update(
            {
                node.name
                for node in classes
                if any(base_name(base) in control_names for base in node.bases)
            }
        )
        if found <= control_names:
            break
        control_names.update(found)
    for path in sorted((root / "packages").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if "/tests/" in relative or "/testing/" in relative or relative in TRUSTED_RECRUITMENT:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        # SDK readers are identified even outside the Discord adapter. HTTP
        # message routes and imported turn-driver aliases are scanned everywhere.
        sdk = "import discord" in source or "from discord" in source
        turn_names = {"run_turn"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module in {"daimon.core.turn.driver", "daimon.core.headless_runner"}
            ):
                turn_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "run_turn"
                )
        owners: list[str] = []

        def visit(node, owners=owners, sdk=sdk, turn_names=turn_names, relative=relative):
            owner = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            if owner:
                owners.append(node.name)
            kinds = []
            if isinstance(node, ast.Call):
                if isinstance(node.func, (ast.Attribute, ast.Name)):
                    primitive = (
                        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                    )
                    if primitive in {
                        "get_routine",
                        "list_routines_for_tenant",
                        "list_routines_missing_agent_name",
                    }:
                        kinds.append("stored_routine")
                    if primitive in {"history", "fetch_message", "fetch_channel"} or (
                        sdk and primitive == "fetch"
                    ):
                        kinds.append(primitive)
                if (isinstance(node.func, ast.Name) and node.func.id in turn_names) or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "run_turn"
                ):
                    kinds.append("run_turn")
                if any(
                    "channels/" in ast.unparse(arg) and "/messages" in ast.unparse(arg)
                    for arg in [*node.args, *(kw.value for kw in node.keywords)]
                ):
                    kinds.append("http_messages")
            if isinstance(node, ast.ClassDef) and any(
                base_name(base) in control_names for base in node.bases
            ):
                kinds.append("persistent_control")
            for kind in kinds:
                # Only the operation (or class bases) is hashed, not its body:
                # new nested readers receive their own independent entry.
                operation = ast.dump(
                    node
                    if isinstance(node, ast.Call)
                    else ast.Tuple(elts=node.bases, ctx=ast.Load()),
                    include_attributes=False,
                )
                digest = hashlib.sha256(operation.encode()).hexdigest()[:12]
                result[f"{relative}:{'.'.join(owners)}:{kind}:{digest}"] += 1
            for child in ast.iter_child_nodes(node):
                visit(child)
            if owner:
                owners.pop()

        visit(tree)
    return result


def test_recognized_discord_readers_match_review_registry():
    registry = json.loads(REGISTRY.read_text())
    actual = discover(ROOT)
    registered = Counter({key: entry["count"] for key, entry in registry["entries"].items()})
    assert actual == registered, (
        f"Unaudited or changed readers: {actual - registered}; stale entries: {registered - actual}"
    )
    for registered_entry in registry["entries"].values():
        assert registered_entry["boundary"] in registry["boundaries"]
    for entry in registry["boundaries"].values():
        boundary_file, boundary_symbol = entry["boundary"].split(":")
        boundary = (ROOT / boundary_file).read_text()
        tree = ast.parse(boundary)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == boundary_symbol
        ]
        assert functions, entry
        assert any(entry["guard"] in ast.unparse(node) for node in functions), entry
        test_file, test_name = entry["regression"].split(":")
        tests = ast.parse((ROOT / test_file).read_text())
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name
            for node in ast.walk(tests)
        ), entry


def test_scanner_detects_new_reader_caller_http_route_and_control(tmp_path):
    package = tmp_path / "packages" / "new_adapter"
    package.mkdir(parents=True)
    (package / "future.py").write_text("""import discord
from daimon.core.turn.driver import run_turn as drive
from discord.ui import View as Renamed
async def future(channel, http):
    await channel.history()
    await channel.fetch_message(1)
    await channel.fetch_channel(2)
    await http.get(f"/channels/{channel.id}/messages")
    await drive()
    await routines.get_routine()
    await routines.list_routines_for_tenant()
class Saved(discord.ui.View):
    pass
class Answer(discord.ui.Modal):
    pass
class Derived(Renamed):
    pass
""")
    assert sum(discover(tmp_path).values()) == 10
