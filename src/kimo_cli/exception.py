from __future__ import annotations


class KimoCLIException(Exception):
    """Base exception class for OpenKimo."""

    pass


class ConfigError(KimoCLIException, ValueError):
    """Configuration error."""

    pass


class AgentSpecError(KimoCLIException, ValueError):
    """Agent specification error."""

    pass


class InvalidToolError(KimoCLIException, ValueError):
    """Invalid tool error."""

    pass


class SystemPromptTemplateError(KimoCLIException, ValueError):
    """System prompt template error."""

    pass


class MCPConfigError(KimoCLIException, ValueError):
    """MCP config error."""

    pass


class MCPRuntimeError(KimoCLIException, RuntimeError):
    """MCP runtime error."""

    pass
