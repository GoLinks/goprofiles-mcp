import os

import fastmcp
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# OAuth discovery env vars with production defaults.
# Scopes are empty until GoProfiles OAuth scopes are defined for this server.
_ISSUER = os.environ.get("GOPROFILES_OAUTH_ISSUER", "https://www.goprofiles.io")
_AUTHORIZE_URL = os.environ.get(
    "GOPROFILES_OAUTH_AUTHORIZE_URL",
    "https://app.goprofiles.io/oauth_authorize.php",
)
_TOKEN_URL = os.environ.get(
    "GOPROFILES_OAUTH_TOKEN_URL",
    "https://api.goprofiles.io/oauth/token",
)
_REVOKE_URL = os.environ.get(
    "GOPROFILES_OAUTH_REVOKE_URL",
    "https://api.goprofiles.io/oauth/revoke",
)
_MCP_RESOURCE_URL = os.environ.get("MCP_RESOURCE_URL", "https://mcp.goprofiles.io")

_SCOPES: list[str] = []

mcp = fastmcp.FastMCP("GoProfiles")

# Register tools here, e.g.:
# from fastmcp.tools.function_tool import FunctionTool
# from mcp.types import ToolAnnotations
# from goprofiles_mcp.tools.example import example_tool
#
# mcp.add_tool(
#     FunctionTool.from_function(
#         example_tool,
#         title="Example tool",
#         annotations=ToolAnnotations(
#             title="Example tool",
#             readOnlyHint=True,
#             destructiveHint=False,
#             idempotentHint=True,
#             openWorldHint=False,
#         ),
#     )
# )


class RequireBearerOnMCP(BaseHTTPMiddleware):
    """Return 401 + WWW-Authenticate on /mcp when no Bearer token is present.

    Without this, MCP clients (e.g. Claude) skip OAuth discovery and treat the
    connector as unauthenticated.
    """

    def __init__(self, app: ASGIApp, resource_metadata_url: str) -> None:
        super().__init__(app)
        self._resource_metadata_url = resource_metadata_url

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/mcp":
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer realm="GoProfiles MCP", '
                            f'resource_metadata="{self._resource_metadata_url}"'
                        )
                    },
                )
        return await call_next(request)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": _MCP_RESOURCE_URL,
            "authorization_servers": [_MCP_RESOURCE_URL],
            "scopes_supported": _SCOPES,
            "bearer_methods_supported": ["header"],
        }
    )


@mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
async def oauth_protected_resource_metadata_mcp(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": f"{_MCP_RESOURCE_URL}/mcp",
            "authorization_servers": [_MCP_RESOURCE_URL],
            "scopes_supported": _SCOPES,
            "bearer_methods_supported": ["header"],
        }
    )


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "issuer": _ISSUER,
            "authorization_endpoint": _AUTHORIZE_URL,
            "token_endpoint": _TOKEN_URL,
            "revocation_endpoint": _REVOKE_URL,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": _SCOPES,
        }
    )
