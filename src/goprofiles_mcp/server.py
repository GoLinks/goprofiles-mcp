import os
from collections.abc import Callable
from typing import Any

import fastmcp
from fastmcp.apps import AppConfig
from fastmcp.apps.config import app_config_to_meta_dict
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp

from goprofiles_mcp.tools.availability import get_availability
from goprofiles_mcp.tools.bravos import (
    create_bravo,
    preview_bravo,
    search_bravo_types,
    search_bravos,
)
from goprofiles_mcp.tools.celebrations import search_celebrations
from goprofiles_mcp.tools.meetings import preview_meeting, schedule_meeting
from goprofiles_mcp.tools.people import get_profile, search_people

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

# Union of scopes this server may request. Individual tools declare a subset via
# securitySchemes — without that, ChatGPT treats every tool as needing all of these.
_SCOPES = [
    "profiles:read",
    "profiles:write",
    "search:read",
    "users:read",
    "bravos:read",
    "bravos:write",
]
# ChatGPT domain verification for mcp.goprofiles.io — same as golinks-mcp's
# hardcoded token, issued by ChatGPT when verifying this connector's domain.
_OPENAI_CHALLENGE_TOKEN = "WwKZ7-3VxG1cjkg5CHUZuTRuNXVFmG13l659w88Vixc"


class _ScopedFunctionTool(FunctionTool):
    """FunctionTool that emits Apps SDK `securitySchemes` on tools/list.

    FastMCP's to_mcp_tool does not forward securitySchemes. Without a per-tool
    declaration, ChatGPT inherits scopes_supported for every tool.
    """

    security_schemes: list[dict[str, Any]] = Field(default_factory=list)

    def to_mcp_tool(self, **overrides: Any) -> MCPTool:
        mcp_tool = super().to_mcp_tool(**overrides)
        schemes = overrides.get("securitySchemes", self.security_schemes)
        if not schemes:
            return mcp_tool
        payload = mcp_tool.model_dump(by_alias=True, exclude_none=True)
        payload["securitySchemes"] = schemes
        return MCPTool.model_validate(payload)


def _oauth2_tool(
    fn: Callable[..., Any],
    *,
    scopes: list[str],
    title: str,
    annotations: ToolAnnotations,
    invoking: str,
    invoked: str,
    app: AppConfig | None = None,
    extra_meta: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> _ScopedFunctionTool:
    """Register-ready tool with oauth2 securitySchemes and ChatGPT status strings.

    `invoking` / `invoked` are ChatGPT Apps SDK status strings (max 64 chars).
    Optional `app` sets `_meta.ui` (resourceUri / visibility) for MCP Apps.
    """
    if len(invoking) > 64 or len(invoked) > 64:
        raise ValueError("ChatGPT toolInvocation status strings must be ≤64 chars")

    meta: dict[str, Any] = {
        "openai/toolInvocation/invoking": invoking,
        "openai/toolInvocation/invoked": invoked,
        **(extra_meta or {}),
    }
    if app is not None:
        meta["ui"] = app_config_to_meta_dict(app)
        if app.resource_uri:
            meta.setdefault("openai/outputTemplate", app.resource_uri)

    # Omit output_schema when unset — passing None disables FastMCP's auto-inferred
    # schema for str-returning tools (ChatGPT then sees no outputSchema).
    from_fn_kwargs: dict[str, Any] = {
        "title": title,
        "annotations": annotations,
        "meta": meta,
    }
    if output_schema is not None:
        from_fn_kwargs["output_schema"] = output_schema
    base = FunctionTool.from_function(fn, **from_fn_kwargs)
    data = base.model_dump()
    data["fn"] = base.fn
    data["security_schemes"] = [{"type": "oauth2", "scopes": list(scopes)}]
    return _ScopedFunctionTool.model_validate(data)


mcp = fastmcp.FastMCP("GoProfiles")

mcp.add_tool(
    _oauth2_tool(
        search_people,
        scopes=["search:read"],
        title="Search people",
        invoking="Searching people…",
        invoked="People search complete",
        annotations=ToolAnnotations(
            title="Search people",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
)

mcp.add_tool(
    _oauth2_tool(
        get_profile,
        scopes=["profiles:read"],
        title="Get profile",
        invoking="Loading profile…",
        invoked="Profile ready",
        annotations=ToolAnnotations(
            title="Get profile",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
)

mcp.add_tool(
    _oauth2_tool(
        search_celebrations,
        scopes=["profiles:read"],
        title="Search celebrations",
        invoking="Searching celebrations…",
        invoked="Celebrations ready",
        annotations=ToolAnnotations(
            title="Search celebrations",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
)

mcp.add_tool(
    _oauth2_tool(
        get_availability,
        scopes=["profiles:read"],
        title="Get availability",
        invoking="Checking availability…",
        invoked="Availability ready",
        annotations=ToolAnnotations(
            title="Get availability",
            readOnlyHint=True,
            destructiveHint=False,
            # The answer changes minute to minute — not safe to cache or replay.
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
)

mcp.add_tool(
    _oauth2_tool(
        search_bravos,
        scopes=["bravos:read"],
        title="Search bravos",
        invoking="Searching bravos…",
        invoked="Bravos ready",
        annotations=ToolAnnotations(
            title="Search bravos",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
)

mcp.add_tool(
    _oauth2_tool(
        search_bravo_types,
        scopes=["bravos:read"],
        title="Search bravo types",
        invoking="Searching bravo types…",
        invoked="Bravo types ready",
        annotations=ToolAnnotations(
            title="Search bravo types",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
)

# Read-only so hosts do not prompt: this stages and previews but sends nothing.
# Splitting it out of create_bravo is what reduces the flow to a single write
# approval instead of one on each call of a single write-annotated tool.
# bravos:read verifies the bid; profiles:read verifies the recipient uid.
mcp.add_tool(
    _oauth2_tool(
        preview_bravo,
        scopes=["bravos:read", "profiles:read"],
        title="Preview bravo",
        invoking="Preparing Bravo preview…",
        invoked="Bravo preview ready",
        annotations=ToolAnnotations(
            title="Preview bravo",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
)

# No `app=AppConfig(...)` here, deliberately: that emits `_meta.ui`, which made
# ChatGPT drop the tool at discovery rather than error. Keep this registration
# shaped exactly like the read-only tools above.
# profiles:read re-verifies the recipient still exists immediately before sending.
mcp.add_tool(
    _oauth2_tool(
        create_bravo,
        scopes=["bravos:write", "profiles:read"],
        title="Create bravo",
        invoking="Sending Bravo…",
        invoked="Bravo finished",
        annotations=ToolAnnotations(
            title="Create bravo",
            readOnlyHint=False,
            # Additive: creates, deletes nothing. Explicit — this hint defaults true.
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
)


# Read-only for the same reason as preview_bravo: this resolves the attendee,
# detects the workspace's calendar provider, and stages the invite, but creates
# nothing — so the flow costs one write approval rather than one per call.
mcp.add_tool(
    _oauth2_tool(
        preview_meeting,
        scopes=["profiles:read"],
        title="Preview meeting",
        invoking="Preparing meeting preview…",
        invoked="Meeting preview ready",
        annotations=ToolAnnotations(
            title="Preview meeting",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
)

# Shaped exactly like create_bravo, and for the same reason: no `app=AppConfig(...)`,
# because that emits `_meta.ui` and made ChatGPT drop the tool at discovery.
# profiles:read re-verifies the attendee still exists immediately before sending.
mcp.add_tool(
    _oauth2_tool(
        schedule_meeting,
        scopes=["profiles:write", "profiles:read"],
        title="Schedule meeting",
        invoking="Creating calendar invite…",
        invoked="Calendar invite created",
        annotations=ToolAnnotations(
            title="Schedule meeting",
            readOnlyHint=False,
            # Additive: creates, deletes nothing. Explicit — this hint defaults true.
            destructiveHint=False,
            idempotentHint=False,
            # Reaches past GoProfiles into Google/Microsoft.
            openWorldHint=True,
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
async def openai_challenge(request: Request) -> PlainTextResponse:
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
