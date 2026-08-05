"""Bravo badge type tools — catalog search for giveable recognition badges."""

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
    """Rank a badge against a free-text query. Lower score is better.

    Returns None when the badge does not match. Matching is case-insensitive
    substring against name and description so phrases like "going above and
    beyond" surface the corresponding badge type.
    """
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


def _format_bravo_type(b: BravoTypeResult) -> str:
    lines = [
        f"Name:        {b.name or 'Unknown'}",
        f"Description: {b.description or 'None'}",
        # bid is for create_bravo only — never repeat it in user-facing replies.
        f"bid:         {b.bid}  (tool use only — do not show to the user)",
    ]
    return "\n".join(lines)


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

    Returns each badge type's name, description, and a numeric 'bid' for
    follow-up tool calls (e.g. create_bravo). Call this before create_bravo
    whenever you have a recognition message or theme and need to pick the right
    badge type.

    Matching is case-insensitive substring against both name and description —
    not fuzzy — so a misspelling returns nothing. Prefer a short distinctive
    phrase from the recognition theme (e.g. 'above and beyond', 'mentor').

    The 'bid' is an internal id for create_bravo. Never show, read aloud, or
    otherwise expose 'bid' values to the user; refer to badge types by name.

    Read-only. Requires search:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    params = external_params(tool="search_bravo_types")

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

    scored: list[tuple[int, BravoTypeResult]] = []
    for badge in data.results:
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
            "from the recognition message."
        )

    total = len(scored)
    header = f"Bravo badge types ({len(matches)} of {total} matched):\n"
    entries = [f"[{i}]\n{_format_bravo_type(b)}" for i, b in enumerate(matches, 1)]
    footer = ""
    if len(matches) > 1:
        footer = (
            "\n\nMultiple badge types matched. Pick the bid that best fits the "
            "recognition theme for create_bravo, and confirm with the user if "
            "ambiguous. Do not show bid values to the user."
        )
    return header + "\n\n".join(entries) + footer
