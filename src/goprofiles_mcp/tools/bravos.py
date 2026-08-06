"""Bravo tools — badge type catalog search and giving bravos"""

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


class CreateBravoResponse(BaseModel):
    status: str = ""
    message: str = ""
    successful_count: int = 0
    failed_count: int = 0
    total_count: int = 0
    comment_id: int | None = None
    scheduled: bool = False


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


def _validate_badge(
    badges: list[BravoTypeResult], bid: int, badge_name: str
) -> BravoTypeResult | str:
    """Ensure bid exists and badge_name matches the catalog (case-insensitive).

    Returns the catalog badge on success, or an error string for the model.
    """
    match = next((b for b in badges if b.bid == bid), None)
    if match is None:
        return (
            "Invalid bid: that badge type was not found in this workspace's "
            "catalog. You MUST call search_bravo_types in THIS conversation and "
            "pass a bid (and exact badge name) from that tool's results. Do not "
            "reuse bids or badge names from other chats, memory, or examples. "
            "Do not show bid values to the user."
        )

    if match.name.strip().lower() != badge_name.strip().lower():
        return (
            f"badge_name does not match bid. Catalog name for this bid is "
            f"'{match.name}'. Call search_bravo_types in THIS conversation, then "
            "pass that result's bid together with its exact Name field as "
            "badge_name. Do not invent or reuse names from other chats. Do not "
            "show bid values to the user."
        )

    return match


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

    REQUIRED before create_bravo in every conversation. Always call this tool in
    the current chat before previewing or sending a Bravo — even if you already
    "know" a badge name like Team Player from another chat, memory, or examples.
    Badge ids (bid) are workspace-specific and must come from this tool's output.

    Returns each badge type's name, description, and a numeric 'bid' for
    create_bravo. Pass that bid plus the exact Name string as badge_name.

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
            "from the recognition message."
        )

    total = len(scored)
    header = f"Bravo badge types ({len(matches)} of {total} matched):\n"
    entries = [f"[{i}]\n{_format_bravo_type(b)}" for i, b in enumerate(matches, 1)]
    footer = (
        "\n\nUse one of these results with create_bravo: copy bid and the exact "
        "Name as badge_name. Do not use any bid/name that did not appear above. "
        "Do not show bid values to the user."
    )
    if len(matches) > 1:
        footer += (
            " Multiple matches — pick the best fit for the recognition theme and "
            "confirm with the user if ambiguous."
        )
    return header + "\n\n".join(entries) + footer


async def create_bravo(
    receiver_uid: Annotated[
        int,
        Field(
            description=(
                "Numeric user id of the recipient from search_people in THIS "
                "conversation. Pass the uid only — never show it to the user. "
                "The giver is always the authenticated user (from the OAuth "
                "token); do not invent or pass a giver uid."
            ),
            ge=1,
        ),
    ],
    bid: Annotated[
        int,
        Field(
            description=(
                "Numeric badge type id from search_bravo_types in THIS "
                "conversation only. Never invent a bid or reuse one from another "
                "chat, memory, or examples. Pass the bid only — never show it to "
                "the user."
            ),
            ge=1,
        ),
    ],
    badge_name: Annotated[
        str,
        Field(
            description=(
                "Exact badge Name string from the same search_bravo_types result "
                "as bid (e.g. 'Team player'). Required so create_bravo can verify "
                "the pair against the live catalog. Do not invent names from "
                "memory or other chats."
            ),
            min_length=1,
        ),
    ],
    message: Annotated[
        str | None,
        Field(
            description=(
                "Recognition message sent with the Bravo (maps to the API "
                "'comment' field). Keep it under 850 characters. Omit when the "
                "user has not supplied text yet — the tool will tell you to ask "
                "whether to draft a message or wait for theirs. Never invent a "
                "message and send it without asking."
            ),
            max_length=850,
        ),
    ] = None,
    confirmed: Annotated[
        bool,
        Field(
            description=(
                "Must stay false until the user has approved the recipient, "
                "badge type, and exact message text. A false call only returns a "
                "preview and does not send. Re-call with confirmed=true only "
                "after they explicitly agree to send."
            ),
        ),
    ] = False,
    ctx: Context | None = None,
) -> str:
    """Give a Bravo badge to a coworker in the user's GoProfiles workspace
    (https://www.goprofiles.io) as the authenticated user.

    Required workflow — do not skip steps:

    1. In THIS conversation, resolve the recipient with search_people
       (receiver_uid) and the badge with search_bravo_types (bid + badge_name).
       Never skip search_bravo_types, even for familiar names like Team Player.
       Never reuse uid/bid/badge_name from other chats, memory, or examples.
       Confirm with the user when either match is ambiguous.
    2. If the user did not supply a recognition message, ask whether they want
       you to draft one for their review or provide the text themselves. Never
       silently invent a message and send it.
    3. Call this tool with confirmed=false (default) to get a catalog-verified
       preview. Show the user recipient name + the verified badge name from the
       tool output (never show uid or bid) and ask: Send this Bravo?
    4. Call again with the same receiver_uid, bid, badge_name, and message, and
       confirmed=true, only after they explicitly approve. Do not claim the Bravo
       was sent unless that call returns a success line (e.g. "bravo sent
       successfully" / Successful: N).

    The giver is always the OAuth-authenticated user from the Bearer token —
    never accept or invent a giver uid. Never show, read aloud, or otherwise
    expose receiver_uid or bid values to the user.

    Not read-only. Requires profiles:write and search:read scopes.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    badges = await _fetch_bravo_types(authorization, tool="create_bravo")
    resolved = _validate_badge(badges, bid, badge_name)
    if isinstance(resolved, str):
        return resolved

    if not message or not message.strip():
        return (
            "Message is required before sending. Ask the user whether they want "
            "you to draft a recognition message for their review, or provide the "
            "text themselves. Do not invent and send a message without asking. "
            "After they choose and the text is ready, call create_bravo again "
            "with that message, the same bid/badge_name from search_bravo_types, "
            "and confirmed=false to preview, then confirmed=true only after they "
            "approve."
        )

    message = message.strip()

    if not confirmed:
        return (
            "Bravo ready to send (NOT sent yet — do not tell the user it was sent).\n"
            f"Verified badge: {resolved.name}\n"
            f"Message:\n{message}\n\n"
            "Show the user a short summary using the recipient name from "
            "search_people and this verified badge name (never show uid or bid). "
            "Ask explicitly: Send this Bravo? Only if they say yes, call "
            "create_bravo again with the same receiver_uid, bid, badge_name, and "
            "message, and confirmed=true."
        )

    params = external_params(tool="create_bravo")

    try:
        response = await http_client.post(
            "/bravos.php",
            params=params,
            data={
                "bid": bid,
                "comment": message,
                "receiver_uids[]": receiver_uid,
            },
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, "/bravos.php")

    data = CreateBravoResponse.model_validate(response.json())

    if data.status == "failed" or data.successful_count == 0:
        detail = data.message.strip() or "The Bravo could not be sent."
        return (
            f"Failed to send Bravo. {detail} Confirm the recipient from "
            "search_people (you cannot give yourself a Bravo) and the badge "
            "type from search_bravo_types in THIS conversation, then try again. "
            "Do not show uid or bid values to the user."
        )

    lines = [
        data.message.strip() or "Bravo sent successfully.",
        f"Badge:      {resolved.name}",
        f"Successful: {data.successful_count}",
    ]
    if data.failed_count:
        lines.append(f"Failed:     {data.failed_count}")
    if data.scheduled:
        lines.append("Note:       This Bravo was scheduled for later delivery.")
    return "\n".join(lines)
