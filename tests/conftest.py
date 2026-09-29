"""Shared test fixtures: a duck-typed fastmcp Context double and an httpx mock router.

The real fastmcp Context requires a live FastMCP server + session to construct.
Every tool in this codebase only ever touches a handful of attributes on it
(``request_context.request.headers``, ``session_id``, ``session``, ``elicit``),
so FakeContext below implements just that surface rather than standing up a
real server for every test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from goprofiles_mcp.client import GOPROFILES_API_URL


class FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = httpx.Headers(headers)


class FakeRequestContext:
    def __init__(self, request: FakeRequest | None):
        self.request = request


class FakeSession:
    def __init__(self, *, supports_elicitation: bool):
        self.supports_elicitation = supports_elicitation

    def check_client_capability(self, _capability: Any) -> bool:
        return self.supports_elicitation


class FakeContext:
    """Duck-typed stand-in for fastmcp.Context, covering only what tools use."""

    def __init__(
        self,
        *,
        authorization: str | None = "Bearer test-token",
        session_id: str = "test-session",
        no_request_context: bool = False,
        supports_elicitation: bool = False,
        elicit_response: Any | Callable[[str], Any] = None,
    ):
        if no_request_context:
            self.request_context: FakeRequestContext | None = None
        else:
            headers = {"authorization": authorization} if authorization else {}
            self.request_context = FakeRequestContext(FakeRequest(headers))
        self.session_id = session_id
        self.session = FakeSession(supports_elicitation=supports_elicitation)
        self._elicit_response = elicit_response

    async def elicit(self, message: str, **_kwargs: Any) -> Any:
        if callable(self._elicit_response):
            return self._elicit_response(message)
        return self._elicit_response


@pytest.fixture
def make_ctx() -> Callable[..., FakeContext]:
    """Factory fixture so each test can customize the context it needs."""
    return FakeContext


@pytest.fixture
def ctx() -> FakeContext:
    """A context with a plain bearer token and no elicitation support — the
    common case for read-only tool tests."""
    return FakeContext()


@pytest.fixture
def api_mock():
    """respx router pre-bound to the GoProfiles API base URL used by every
    tool's shared http_client."""
    with respx.mock(base_url=GOPROFILES_API_URL, assert_all_called=False) as router:
        yield router
