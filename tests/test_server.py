from __future__ import annotations

import pytest
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from goprofiles_mcp.server import (
    _SCOPES,
    RequireBearerOnMCP,
    _authorization_server_metadata,
    _oauth2_tool,
    _ScopedFunctionTool,
    mcp,
)

# mcp.http_app() gives the real Starlette app with every @mcp.custom_route
# registered, so the well-known/health routes can be hit through an ordinary
# TestClient. RequireBearerOnMCP itself is only wired up by __main__.py (as
# ASGI middleware passed to run_http_async), not by http_app(), so it's tested
# below by constructing the middleware directly against a trivial app.
app = mcp.http_app(path="/mcp")
client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class TestOauthProtectedResource:
    def test_root_metadata(self):
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        body = response.json()
        assert body["scopes_supported"] == _SCOPES
        assert body["bearer_methods_supported"] == ["header"]
        assert body["authorization_servers"] == [body["resource"]]

    def test_mcp_metadata(self):
        response = client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 200
        body = response.json()
        assert body["scopes_supported"] == _SCOPES
        assert body["bearer_methods_supported"] == ["header"]
        assert body["resource"].endswith("/mcp")
        assert body["authorization_servers"] == [body["resource"].removesuffix("/mcp")]


class TestAuthorizationServerMetadata:
    def test_matches_module_helper(self):
        expected = _authorization_server_metadata()
        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json() == expected

    def test_shape(self):
        body = _authorization_server_metadata()
        assert body["response_types_supported"] == ["code"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["token_endpoint_auth_methods_supported"] == ["none"]
        assert body["scopes_supported"] == _SCOPES
        assert body["issuer"]
        assert body["authorization_endpoint"]
        assert body["token_endpoint"]
        assert body["revocation_endpoint"]


class TestRequireBearerOnMCP:
    RESOURCE_METADATA_URL = (
        "https://example.test/.well-known/oauth-protected-resource/mcp"
    )

    def _client(self):
        async def downstream(scope, receive, send):
            response = PlainTextResponse("ok")
            await response(scope, receive, send)

        app = RequireBearerOnMCP(
            downstream, resource_metadata_url=self.RESOURCE_METADATA_URL
        )
        return TestClient(app)

    def test_missing_authorization_header_returns_401(self):
        response = self._client().get("/mcp")
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        www_auth = response.headers["www-authenticate"]
        assert 'Bearer realm="GoProfiles MCP"' in www_auth
        assert self.RESOURCE_METADATA_URL in www_auth

    def test_trailing_slash_is_also_gated(self):
        response = self._client().get("/mcp/", follow_redirects=False)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

    def test_non_bearer_scheme_returns_401(self):
        response = self._client().get("/mcp", headers={"authorization": "Basic abc123"})
        assert response.status_code == 401

    def test_bearer_prefix_is_case_insensitive(self):
        response = self._client().get(
            "/mcp", headers={"authorization": "bearer sometoken"}
        )
        assert response.status_code == 200
        assert response.text == "ok"

    def test_unrelated_path_is_not_gated(self):
        response = self._client().get("/other")
        assert response.status_code == 200
        assert response.text == "ok"


async def _trivial_tool(x: int) -> int:
    return x


def _build_scoped_tool(
    security_schemes: list[dict] | None = None,
) -> _ScopedFunctionTool:
    base = FunctionTool.from_function(
        _trivial_tool, title="Trivial", annotations=ToolAnnotations(title="Trivial")
    )
    data = base.model_dump()
    data["fn"] = base.fn
    if security_schemes is not None:
        data["security_schemes"] = security_schemes
    return _ScopedFunctionTool.model_validate(data)


class TestScopedFunctionTool:
    def test_empty_security_schemes_omits_key(self):
        tool = _build_scoped_tool()
        assert tool.security_schemes == []
        mcp_tool = tool.to_mcp_tool()
        assert isinstance(mcp_tool, MCPTool)
        dumped = mcp_tool.model_dump(by_alias=True, exclude_none=True)
        assert "securitySchemes" not in dumped

    def test_security_schemes_are_included(self):
        schemes = [{"type": "oauth2", "scopes": ["profiles:read"]}]
        tool = _build_scoped_tool(schemes)
        dumped = tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)
        assert dumped["securitySchemes"] == schemes


class TestOauth2Tool:
    def _kwargs(self, **overrides):
        kwargs = {
            "scopes": ["profiles:read"],
            "title": "Trivial",
            "annotations": ToolAnnotations(title="Trivial"),
            "invoking": "Working…",
            "invoked": "Done",
        }
        kwargs.update(overrides)
        return kwargs

    def test_sets_oauth2_security_scheme(self):
        tool = _oauth2_tool(_trivial_tool, **self._kwargs(scopes=["profiles:read"]))
        assert tool.security_schemes == [
            {"type": "oauth2", "scopes": ["profiles:read"]}
        ]

    def test_invoking_over_64_chars_raises(self):
        with pytest.raises(ValueError, match="≤64 chars"):
            _oauth2_tool(_trivial_tool, **self._kwargs(invoking="x" * 65))

    def test_invoked_over_64_chars_raises(self):
        with pytest.raises(ValueError, match="≤64 chars"):
            _oauth2_tool(_trivial_tool, **self._kwargs(invoked="x" * 65))

    def test_exactly_64_chars_is_allowed(self):
        tool = _oauth2_tool(
            _trivial_tool, **self._kwargs(invoking="x" * 64, invoked="y" * 64)
        )
        assert tool is not None


async def test_all_expected_tools_are_registered():
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "search_people",
        "get_profile",
        "search_celebrations",
        "get_availability",
        "search_bravos",
        "search_bravo_types",
        "preview_bravo",
        "create_bravo",
        "preview_meeting",
        "schedule_meeting",
    }
    assert len(tools) == 10
