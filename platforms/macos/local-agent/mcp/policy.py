from __future__ import annotations

from local_agent.mcp.config import MCPServerConfig, RiskClass, ToolPolicy
from local_agent.mcp.errors import (
    MCPIntentMismatchError,
    MCPIntentRequiredError,
    MCPToolDeniedError,
    MCPUnknownToolError,
)


def authorize_tool(
    server: MCPServerConfig,
    tool_name: str,
    *,
    intent: str | None,
) -> ToolPolicy:
    """Authorize one exact configured tool without trusting server-provided hints."""
    policy = server.tool_policy(tool_name)
    if policy is None:
        raise MCPUnknownToolError(
            f"tool {tool_name!r} has no local policy for MCP server {server.server_id!r}"
        )
    if not policy.enabled:
        raise MCPToolDeniedError(
            f"tool {tool_name!r} is disabled for MCP server {server.server_id!r}"
        )

    if policy.risk is RiskClass.READ:
        if intent not in (None, RiskClass.READ.value):
            raise MCPIntentMismatchError(
                f"tool {tool_name!r} is classified read but invocation intent is {intent!r}"
            )
        return policy

    required = policy.risk.value
    if intent is None:
        raise MCPIntentRequiredError(
            f"tool {tool_name!r} is classified {required}; explicit --intent {required} is required"
        )
    if intent != required:
        raise MCPIntentMismatchError(
            f"tool {tool_name!r} requires intent {required!r}, got {intent!r}"
        )
    return policy
