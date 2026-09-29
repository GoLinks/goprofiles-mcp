import httpx
import pytest

from goprofiles_mcp.client import (
    api_get,
    external_params,
    format_timestamp,
    get_authorization_header,
    raise_for_status,
)


class TestExternalParams:
    # A dev .env with GOPROFILES_EXTERNAL_REQUEST=true is common locally (it's
    # gitignored, loaded once at import time), so every test here pins the flag
    # explicitly rather than relying on whatever happens to be in the environment.

    def test_tags_source_and_tool(self, monkeypatch):
        monkeypatch.setattr("goprofiles_mcp.client.GOPROFILES_EXTERNAL_REQUEST", False)
        params = external_params(tool="search_people")
        assert params == {"source": "mcp", "mcp_tool": "search_people"}

    def test_merges_extra_params(self, monkeypatch):
        monkeypatch.setattr("goprofiles_mcp.client.GOPROFILES_EXTERNAL_REQUEST", False)
        params = external_params({"uid": 5}, tool="get_profile")
        assert params == {"source": "mcp", "mcp_tool": "get_profile", "uid": 5}

    def test_extra_does_not_override_reserved_keys(self, monkeypatch):
        # source/mcp_tool are always the real values: extra is spread first, then
        # the reserved keys overwrite any colliding caller values.
        monkeypatch.setattr("goprofiles_mcp.client.GOPROFILES_EXTERNAL_REQUEST", False)
        params = external_params(
            {"source": "spoofed", "mcp_tool": "spoofed_tool", "uid": 5},
            tool="get_profile",
        )
        assert params == {"source": "mcp", "mcp_tool": "get_profile", "uid": 5}

    def test_external_request_flag(self, monkeypatch):
        monkeypatch.setattr("goprofiles_mcp.client.GOPROFILES_EXTERNAL_REQUEST", True)
        params = external_params(tool="get_profile")
        assert params["externalRequest"] == "true"

    def test_no_external_request_flag_by_default(self, monkeypatch):
        monkeypatch.setattr("goprofiles_mcp.client.GOPROFILES_EXTERNAL_REQUEST", False)
        params = external_params(tool="get_profile")
        assert "externalRequest" not in params


class TestRaiseForStatus:
    @pytest.mark.parametrize("status", [200, 201])
    def test_success_statuses_do_not_raise(self, status):
        response = httpx.Response(status)
        raise_for_status(response, "test-api")

    def test_401_raises_permission_error(self):
        response = httpx.Response(401)
        with pytest.raises(PermissionError):
            raise_for_status(response, "test-api")

    def test_403_raises_permission_error_with_body(self):
        response = httpx.Response(403, text="insufficient scope")
        with pytest.raises(PermissionError, match="insufficient scope"):
            raise_for_status(response, "test-api")

    def test_404_raises_lookup_error_with_default_message(self):
        response = httpx.Response(404)
        with pytest.raises(LookupError, match="Not found: test-api"):
            raise_for_status(response, "test-api")

    def test_404_raises_lookup_error_with_custom_message(self):
        response = httpx.Response(404)
        with pytest.raises(LookupError, match="custom not-found message"):
            raise_for_status(
                response, "test-api", not_found_message="custom not-found message"
            )

    def test_409_raises_value_error(self):
        response = httpx.Response(409, text="conflict detail")
        with pytest.raises(ValueError, match="Conflict"):
            raise_for_status(response, "test-api")

    def test_422_raises_value_error(self):
        response = httpx.Response(422, text="bad field")
        with pytest.raises(ValueError, match="Validation error"):
            raise_for_status(response, "test-api")

    def test_429_raises_runtime_error(self):
        response = httpx.Response(429)
        with pytest.raises(RuntimeError, match="rate limit"):
            raise_for_status(response, "test-api")

    def test_500_raises_generic_runtime_error(self):
        response = httpx.Response(500, text="boom")
        with pytest.raises(RuntimeError, match="status 500"):
            raise_for_status(response, "test-api")


class TestApiGet:
    async def test_returns_response_on_success(self, api_mock):
        api_mock.get("/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        response = await api_get("/ping", {}, "Bearer tok")
        assert response.json() == {"ok": True}

    async def test_maps_timeout_to_timeout_error(self, api_mock):
        api_mock.get("/ping").mock(side_effect=httpx.TimeoutException("slow"))
        with pytest.raises(TimeoutError):
            await api_get("/ping", {}, "Bearer tok")

    async def test_maps_connect_error_to_connection_error(self, api_mock):
        api_mock.get("/ping").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ConnectionError):
            await api_get("/ping", {}, "Bearer tok")

    async def test_maps_404_to_lookup_error_with_custom_message(self, api_mock):
        api_mock.get("/ping").mock(return_value=httpx.Response(404))
        with pytest.raises(LookupError, match="nobody home"):
            await api_get("/ping", {}, "Bearer tok", not_found_message="nobody home")

    async def test_sends_authorization_header(self, api_mock):
        route = api_mock.get("/ping").mock(return_value=httpx.Response(200, json={}))
        await api_get("/ping", {}, "Bearer secret-token")
        assert (
            route.calls.last.request.headers["authorization"] == "Bearer secret-token"
        )


class TestFormatTimestamp:
    def test_none_is_unknown(self):
        assert format_timestamp(None) == "Unknown"

    def test_formats_utc(self):
        assert format_timestamp(1_700_000_000) == "2023-11-14 22:13 UTC"


class TestGetAuthorizationHeader:
    def test_missing_request_context_raises(self, make_ctx):
        with pytest.raises(PermissionError, match="Missing request context"):
            get_authorization_header(make_ctx(no_request_context=True))

    def test_missing_bearer_prefix_raises(self, make_ctx):
        with pytest.raises(PermissionError, match="Missing bearer token"):
            get_authorization_header(make_ctx(authorization="Basic abc123"))

    def test_missing_header_raises(self, make_ctx):
        with pytest.raises(PermissionError, match="Missing bearer token"):
            get_authorization_header(make_ctx(authorization=None))

    def test_returns_header_value(self, make_ctx):
        header = get_authorization_header(make_ctx(authorization="Bearer abc123"))
        assert header == "Bearer abc123"

    def test_case_insensitive_bearer_prefix(self, make_ctx):
        header = get_authorization_header(make_ctx(authorization="bearer abc123"))
        assert header == "bearer abc123"
