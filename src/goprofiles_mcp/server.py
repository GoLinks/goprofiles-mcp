import os

import fastmcp
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp

from goprofiles_mcp.tools.people import search_people

# OAuth discovery env vars with production defaults
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

_SCOPES = ["profiles:read", "profiles:write", "search:read", "users:read"]
# ChatGPT domain verification for mcp.goprofiles.io — same role as golinks-mcp's
# hardcoded token. Set via ECS env, or paste the token ChatGPT shows when verifying
# the connector domain.
_OPENAI_CHALLENGE_TOKEN = os.environ.get("OPENAI_APPS_CHALLENGE_TOKEN", "")

mcp = fastmcp.FastMCP("GoProfiles")

mcp.add_tool(
    FunctionTool.from_function(
        search_people,
        title="Search people",
        annotations=ToolAnnotations(
            title="Search people",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
)


class RequireBearerOnMCP(BaseHTTPMiddleware):
    """Return 401 + WWW-Authenticate on /mcp when no Bearer token is present.

    Without this, MCP clients (e.g. Claude) skip OAuth discovery and treat the
    connector as unauthenticated.
    """

    def __init__(self, app: ASGIApp, resource_metadata_url: str) -> None:
        super().__init__(app)
        self._resource_metadata_url = resource_metadata_url

    async def dispatch(self, request: Request, call_next) -> Response:
        # rstrip so "/mcp/" is gated too — an exact match let the trailing-slash
        # form skip the challenge and fall straight through to a redirect.
        if request.url.path.rstrip("/") == "/mcp":
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


@mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
async def openai_challenge(request: Request) -> Response:
    """Domain verification for ChatGPT custom connectors (same pattern as golinks-mcp)."""
    # 404 rather than an empty 200 when unset: serving a blank body reads as a
    # successful verification with the wrong token, which is harder to diagnose.
    if not _OPENAI_CHALLENGE_TOKEN:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return PlainTextResponse(_OPENAI_CHALLENGE_TOKEN)


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


def _authorization_server_metadata() -> dict:
    """OAuth AS metadata (RFC 8414). Also served at openid-configuration for clients
    that probe the OIDC discovery path first (e.g. ChatGPT connectors)."""
    return {
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


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse(_authorization_server_metadata())


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
async def openid_configuration(request: Request) -> JSONResponse:
    return JSONResponse(_authorization_server_metadata())
