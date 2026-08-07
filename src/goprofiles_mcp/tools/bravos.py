"""Bravo tools — badge catalog search."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Annotated

import httpx
from fastmcp import Context
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    external_params,
    get_authorization_header,
    http_client,
    raise_for_status,
)

# Tokens are only printed by search_bravo_types, so a caller holding one proves
# it read this tool's output rather than inventing a badge.
_BADGE_TOKEN_SECRET = os.environ.get(
    "GOPROFILES_MCP_BADGE_TOKEN_SECRET", "goprofiles-mcp-badge-token"
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BravoTypeResult(BaseModel):
    bid: int = 0
    name: str = ""
    description: str = ""


class BravoTypesResponse(BaseModel):
    results: list[BravoTypeResult] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _match_score(badge: BravoTypeResult, query: str) -> int | None:
    """Rank a badge against a free-text query. Lower score is better."""
    q = query.strip().lower()
    if not q:
        return 0

    name = badge.name.lower()
    description = badge.description.lower()

    if name == q:
        return 0
    if name.startswith(q):
        return 1
    if q in name:
        return 2
    if q in description:
        return 3
    return None


def _badge_token(bid: int, name: str) -> str:
    """Opaque token proving bid/name came from search_bravo_types output."""
    payload = f"{bid}\n{name.strip().lower()}".encode()
    digest = hmac.new(
        _BADGE_TOKEN_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return digest[:24]


def _format_bravo_type(b: BravoTypeResult) -> str:
    token = _badge_token(b.bid, b.name)
    lines = [
        f"Name:        {b.name or 'Unknown'}",
        f"Description: {b.description or 'None'}",
        # bid / badge_token are for follow-up tool calls — never show to the user.
        f"bid:         {b.bid}  (tool use only — do not show to the user)",
        f"badge_token: {token}  (tool use only — do not show to the user)",
    ]
    return "\n".join(lines)


async def _fetch_bravo_types(authorization: str, *, tool: str) -> list[BravoTypeResult]:
    """Load the giveable badge catalog from GET /bravos.php."""
    params = external_params(tool=tool)

    try:
        response = await http_client.get(
            "/bravos.php",
            params=params,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, "/bravos.php")
    data = BravoTypesResponse.model_validate(response.json())
    return data.results


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search_bravo_types(
    search: Annotated[
        str,
        Field(
            description=(
                "Free-text phrase matched against badge type name and description "
                "(case-insensitive substring). Examples: 'going above and beyond', "
                "'team player', 'collaborat'. Use words that describe the recognition "
                "you want to give."
            ),
            min_length=1,
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of matching badge types to return (1–100).",
            ge=1,
            le=100,
        ),
    ] = 20,
    ctx: Context | None = None,
) -> str:
    """Search the catalog of giveable Bravo badge types in the user's GoProfiles
    workspace (https://www.goprofiles.io).

    Always call this tool in the current chat before acting on a badge type —
    even if you already "know" a badge name like Team Player from another chat,
    memory, or examples. Badge ids (bid) are workspace-specific and must come
    from this tool's output.

    Returns each badge type's name, description, a numeric 'bid', and a
    badge_token.

    After results return, list the badge Name(s) to the user and ask which bravo
    type they want — even when there is only one match.

    The 'bid' and 'badge_token' are internal. Never show, read aloud, or
    otherwise expose them to the user; refer to badge types by name.

    Read-only. Requires search:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    badges = await _fetch_bravo_types(authorization, tool="search_bravo_types")

    scored: list[tuple[int, BravoTypeResult]] = []
    for badge in badges:
        score = _match_score(badge, search)
        if score is None:
            continue
        scored.append((score, badge))

    scored.sort(key=lambda item: (item[0], item[1].name.lower()))
    matches = [badge for _, badge in scored[:limit]]

    if not matches:
        return (
            "No bravo badge types matched that search. Matching is substring-based "
            "against name and description — retry with a shorter phrase "
            "(e.g. 'above and beyond' or 'collaborat'), or try a different theme "
            "from the recognition message. Tell the user nothing matched and ask "
            "how they'd like to refine the search."
        )

    total = len(scored)
    names = [b.name or "Unknown" for b in matches]
    name_list = ", ".join(f"'{n}'" for n in names)
    header = f"Bravo badge types ({len(matches)} of {total} matched):\n"
    entries = [f"[{i}]\n{_format_bravo_type(b)}" for i, b in enumerate(matches, 1)]
    footer = (
        "\n\nNext step:\n"
        f"1. Tell the user these matching bravo type name(s): {name_list}.\n"
        "2. Ask which one they want to use"
        + (
            " (or confirm the single match)."
            if len(matches) == 1
            else ", and wait for their choice."
        )
        + "\n"
        "Do not invent or reuse values that did not appear above. Do not show "
        "bid or badge_token to the user."
    )
    return header + "\n\n".join(entries) + footer
