"""Typed errors for the token broker.

Adapter boundaries (MCP tool, CLI commands) catch these and convert to
their adapter-native error responses. Core code raises them, never
swallows them.
"""

from __future__ import annotations

from daimon.core.errors import DaimonError


class BrokerError(DaimonError):
    """Base for token-broker errors. Adapters catch this at their edge."""


class NoBindingError(BrokerError):
    """Raised when no credential / binding exists for the requesting agent.

    Resolution: connect the required external account for this agent.
    """


class ProviderConfigError(BrokerError):
    """Raised when provider configuration (settings) is missing or invalid.

    Operator-actionable: the resolution is to set the missing deployment
    configuration value.
    """
