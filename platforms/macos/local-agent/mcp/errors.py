from __future__ import annotations

from typing import Any


class MCPBoundaryError(RuntimeError):
    """Expected fail-closed error at the Local Agent MCP boundary."""

    code = "mcp_error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
        }


class MCPConfigError(MCPBoundaryError):
    code = "invalid_registry"


class MCPArgumentsError(MCPBoundaryError):
    code = "invalid_arguments"


class MCPUnknownServerError(MCPBoundaryError):
    code = "unknown_server"


class MCPServerDisabledError(MCPBoundaryError):
    code = "server_disabled"


class MCPUnknownToolError(MCPBoundaryError):
    code = "unknown_tool"


class MCPToolDeniedError(MCPBoundaryError):
    code = "tool_denied"


class MCPIntentRequiredError(MCPBoundaryError):
    code = "intent_required"


class MCPIntentMismatchError(MCPBoundaryError):
    code = "intent_mismatch"


class MCPConnectionTimeoutError(MCPBoundaryError):
    code = "connection_timeout"


class MCPCallTimeoutError(MCPBoundaryError):
    code = "call_timeout"


class MCPTransportError(MCPBoundaryError):
    code = "transport_error"


class MCPProtocolError(MCPBoundaryError):
    code = "protocol_error"


class MCPResultTooLargeError(MCPBoundaryError):
    code = "result_too_large"


class MCPArtifactError(MCPBoundaryError):
    code = "artifact_error"
