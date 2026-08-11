import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastmcp import Context

# Sort direction shared across all list/search endpoints
SortOrder = Literal["asc", "desc"]

# Load repo-root .env when present (local hosting). No-op on ECS / prod images
# where the file is absent. Does not override vars already set in the process env.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

GOPROFILES_API_URL = os.environ.get("GOPROFILES_API_URL", "https://api.goprofiles.io")
GOPROFILES_EXTERNAL_REQUEST = (
    os.environ.get("GOPROFILES_EXTERNAL_REQUEST", "").lower() == "true"
)

http_client = httpx.AsyncClient(
    base_url=GOPROFILES_API_URL,
    timeout=30,
)

REQUEST_SOURCE = "mcp"


def external_params(extra: dict | None = None, *, tool: str) -> dict:
    """Return query params, always tagging the MCP request source & the calling tool,
    and adding externalRequest=true when the flag is set."""
    params: dict = {"source": REQUEST_SOURCE, "mcp_tool": tool, **(extra or {})}
    if GOPROFILES_EXTERNAL_REQUEST:
        params = {"externalRequest": "true", **params}
    return params


def raise_for_status(
    response: httpx.Response, api_label: str, *, not_found_message: str | None = None
) -> None:
    """Translate GoProfiles API HTTP errors into typed Python exceptions.

    Treats 200 and 201 as success; everything else raises.
    """
    if response.status_code in (200, 201):
        return
    if response.status_code == 401:
        raise PermissionError("Invalid or expired access token.")
    if response.status_code == 403:
        raise PermissionError(
            f"Access denied: insufficient scope or permissions. {response.text[:200]}"
        )
    if response.status_code == 404:
        raise LookupError(not_found_message or f"Not found: {api_label}.")
    if response.status_code == 409:
        raise ValueError(f"Conflict: {response.text[:300]}")
    if response.status_code == 422:
        raise ValueError(f"Validation error: {response.text[:300]}")
    if response.status_code == 429:
        raise RuntimeError("GoProfiles rate limit exceeded. Please try again later.")
    raise RuntimeError(
        f"GoProfiles {api_label} API returned status {response.status_code}: "
        f"{response.text[:500]}"
    )


def format_timestamp(ts: int | None) -> str:
    """Format a Unix timestamp (seconds) as 'YYYY-MM-DD HH:MM UTC'."""
    if ts is None:
        return "Unknown"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def get_authorization_header(ctx: Context) -> str:
    """Return the incoming request Authorization header for forwarding to GoProfiles APIs."""
    if ctx.request_context is None or ctx.request_context.request is None:
        raise PermissionError("Missing request context.")

    authorization = ctx.request_context.request.headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise PermissionError("Missing bearer token.")

    return authorization
