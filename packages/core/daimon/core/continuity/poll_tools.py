"""Code-reviewed OS polling catalogue. Never infer enrollment from a tool name.

Entries are reviewed with their adapter implementation and live authorization.
The READ and NO_WATCH flags are independent: a read may be unsuitable to poll.
No URL, shell, management, model execution, clock or external MCP tool belongs
here. Expanding this registry requires code review and authorization tests.
"""

from dataclasses import dataclass
from types import MappingProxyType

from daimon.core.continuity.watch_projection import bounded, projection_path
from pydantic import JsonValue


@dataclass(frozen=True)
class PollTool:
    effect: str
    no_watch: bool
    arguments: frozenset[str]


POLL_TOOLS = MappingProxyType(
    {
        "read_thread": PollTool("read", False, frozenset({"thread_id", "limit"})),
        "read_channel": PollTool("read", False, frozenset({"channel_id", "limit"})),
    }
)


def require_poll_tool(name: str) -> PollTool:
    tool = POLL_TOOLS.get(name)
    if tool is None or tool.effect != "read" or tool.no_watch:
        raise ValueError("Tool is not reviewed for polling")
    return tool


def validate_poll(
    name: str, arguments: dict[str, JsonValue], *, projection: str, rule: str, interval: int
) -> None:
    tool = require_poll_tool(name)
    bounded(arguments)
    projection_path(projection)
    if set(arguments) - tool.arguments or rule not in {"change", "added", "updated"}:
        raise ValueError("Unsupported poll arguments or rule")
    identity = "thread_id" if name == "read_thread" else "channel_id"
    value = arguments.get(identity)
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 24:
        raise ValueError("Poll source requires a Discord resource ID")
    limit = arguments.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Poll read limit must be 1 to 20")
    if not 60 <= interval <= 86400:
        raise ValueError("Poll interval must be 60 to 86400 seconds")
