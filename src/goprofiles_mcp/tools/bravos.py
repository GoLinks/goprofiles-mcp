"""Bravo tools — badge catalog search and confirmed Bravo creation."""

from __future__ import annotations

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
from goprofiles_mcp.confirmations import ClaimStatus, claim, stage

_TOOL = "create_bravo"

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


def _format_bravo_type(b: BravoTypeResult) -> str:
    lines = [
        f"Name:        {b.name or 'Unknown'}",
        f"Description: {b.description or 'None'}",
        # bid is for follow-up tool calls — never show it to the user.
        f"bid:         {b.bid}  (tool use only — do not show to the user)",
    ]
    return "\n".join(lines)


async def _recipient_name(uid: int, authorization: str) -> str | None:
    """Confirm a uid exists and return a display name, or None if it doesn't.

    Same two-layer check get_profile uses: /users.php 404s via LookupError, but
    it can also answer 200 with a body that carries no uid.
    """
    params = external_params({"uid": uid}, tool=_TOOL)

    try:
        response = await http_client.get(
            "/users.php",
            params=params,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    try:
        raise_for_status(response, "/users.php")
    except LookupError:
        return None

    raw = response.json()
    if not isinstance(raw, dict) or not raw.get("uid"):
        return None

    first = str(raw.get("first_name") or "").strip()
    last = str(raw.get("last_name") or "").strip()
    return f"{first} {last}".strip() or str(raw.get("username") or "").strip() or "Unknown"


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

    Returns each badge type's name, description, and a numeric 'bid' for
    create_bravo.

    After results return, ask the user for everything still missing in one
    message — which badge (unless they already named one of the matches) and what
    the recognition message should say. Do not ask for these one turn at a time.

    The 'bid' is internal. Never show, read aloud, or otherwise expose it to the
    user; refer to badge types by name.

    Read-only. Requires bravos:read scope.
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
    # Ask for everything still missing in ONE turn. Asking for the badge and the
    # message in separate turns made a three-field action take four exchanges.
    footer = (
        "\n\nNext step — ask for everything you still need in a SINGLE message:\n"
        f"- Matching bravo type name(s): {name_list}.\n"
        "- If the user already named one of these badges, treat that as their "
        "choice and do NOT ask them to confirm it again.\n"
        "- Otherwise ask which one they want.\n"
        "- In the same message, unless they already gave you the recognition "
        "message, ask what it should say (and offer to draft one).\n"
        "Once you have the recipient, the badge, and the message, call "
        "preview_bravo once.\n"
        "Do not invent or reuse values that did not appear above. Do not show "
        "bid values to the user."
    )
    return header + "\n\n".join(entries) + footer


async def preview_bravo(
    receiver_uid: Annotated[
        int,
        Field(
            description=(
                "Numeric user id of the recipient, from search_people in THIS "
                "conversation. Pass the uid only — never show it to the user. The "
                "sender is always the authenticated user; there is no sender "
                "parameter and you must not invent one."
            ),
            ge=1,
        ),
    ],
    bid: Annotated[
        int,
        Field(
            description=(
                "Numeric badge type id from search_bravo_types in THIS "
                "conversation. Never invent a bid or reuse one from another chat, "
                "memory, or examples. Never show it to the user."
            ),
            ge=1,
        ),
    ],
    comment: Annotated[
        str,
        Field(
            description=(
                "The recognition message to send (max 850 characters). Agree this "
                "with the user before calling — either they supply it or you draft "
                "it for their review."
            ),
            min_length=1,
            max_length=850,
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Preview a Bravo before sending it. Sends nothing.

    Resolves the recipient and badge to their real names, checks both exist, and
    returns a preview plus a confirmation_token for create_bravo. Nothing is sent
    and nothing is notified.

    Call this once you have all three of: the recipient, the badge, and the
    message. Gather the badge and the message together in a single question
    rather than one at a time — do not call this tool with a message you invented
    and have not shown the user.

    Then show the user the To / Badge / Message from the result and wait for them
    to explicitly approve it. Only after that, call create_bravo.

    Read-only. Requires bravos:read and profiles:read scopes.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    comment = comment.strip()
    if not comment:
        return (
            "No preview — the message is empty. Ask the user what they want it to "
            "say, or offer to draft something for their review."
        )

    badges = await _fetch_bravo_types(authorization, tool="preview_bravo")
    badge = next((b for b in badges if b.bid == bid), None)
    if badge is None:
        return (
            "No preview — that badge type is not in this workspace's catalog. Call "
            "search_bravo_types in THIS conversation and use a bid from its "
            "results. Do not reuse bids from other chats, memory, or examples."
        )

    recipient = await _recipient_name(receiver_uid, authorization)
    if recipient is None:
        return (
            "No preview — no person found with that uid. Confirm the recipient "
            "with search_people and try again with the uid from its results."
        )

    token = stage(
        tool=_TOOL,
        # Executed on ids the model never resends, so the send call cannot be
        # redirected to a different person or badge.
        payload={"receiver_uid": receiver_uid, "bid": bid, "comment": comment},
        confirm_args={
            "recipient_name": recipient,
            "badge_name": badge.name,
            "comment": comment,
        },
    )

    return (
        "Bravo previewed — NOT sent.\n\n"
        f"To:      {recipient}\n"
        f"Badge:   {badge.name}\n"
        "Message:\n"
        f"{comment}\n\n"
        f"confirmation_token: {token}  (tool use only — do not show to the user)\n\n"
        "NEXT STEP — show the user the To / Badge / Message above and ask them to "
        "confirm. Do not call create_bravo until they explicitly say to send it. "
        "When they do, call create_bravo with recipient_name, badge_name, and "
        "comment copied exactly from this preview, plus this confirmation_token. "
        "create_bravo takes no uid or bid."
    )


async def create_bravo(
    recipient_name: Annotated[
        str,
        Field(
            description=(
                "The recipient's name exactly as it appeared in the preview_bravo "
                "result ('To:'). Copy it verbatim. This is shown to the user when "
                "they approve the send, and is checked against the preview."
            ),
            min_length=1,
        ),
    ],
    badge_name: Annotated[
        str,
        Field(
            description=(
                "The badge name exactly as it appeared in the preview_bravo result "
                "('Badge:'). Copy it verbatim — checked against the preview."
            ),
            min_length=1,
        ),
    ],
    comment: Annotated[
        str,
        Field(
            description=(
                "The recognition message exactly as it appeared in the "
                "preview_bravo result ('Message:'). Copy it verbatim — checked "
                "against the preview. Do not shorten, summarize, or reword it."
            ),
            min_length=1,
            max_length=850,
        ),
    ],
    confirmation_token: Annotated[
        str,
        Field(
            description=(
                "The confirmation_token from preview_bravo. Required — there is no "
                "way to send a Bravo without previewing it first. Never show it to "
                "the user."
            ),
            min_length=1,
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Send a Bravo (peer recognition) that the user has already approved.

    ONLY call this after preview_bravo and after the user has explicitly said to
    send that preview. The user asking for a Bravo is not approval of its
    contents — they must approve the actual recipient, badge, and message first.

    Sends the recipient and badge resolved by preview_bravo, so this tool takes
    no uid or bid: the arguments here are the human-readable values the user
    approved, and they are checked against the preview rather than sent.

    The sender is always the authenticated user, derived from the access token.

    Not read-only and not reversible — it notifies the recipient. Requires
    bravos:write and profiles:read scopes.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    result = await claim(
        ctx,
        confirmation_token,
        tool=_TOOL,
        confirm_args={
            "recipient_name": recipient_name,
            "badge_name": badge_name,
            "comment": comment.strip(),
        },
        summary=(
            "Send this Bravo?\n\n"
            f"To: {recipient_name}\n"
            f"Badge: {badge_name}\n\n"
            f"{comment.strip()}"
        ),
    )

    if result.status is ClaimStatus.DECLINED:
        return (
            "No Bravo sent — the user declined. Ask what they'd like to change. "
            "The confirmation_token is still valid if they only needed a moment; "
            "otherwise call preview_bravo again with the new details."
        )
    if result.status is ClaimStatus.DRIFTED:
        return (
            "No Bravo sent — the recipient, badge, or message does not match the "
            "preview. Copy recipient_name, badge_name, and comment verbatim from "
            "the preview_bravo result, or call preview_bravo again if the user "
            "wants different content. This token is still valid."
        )
    if result.status is ClaimStatus.EXPIRED:
        return (
            "No Bravo sent — the confirmation expired. Call preview_bravo again, "
            "then re-confirm with the user."
        )
    if not result.ok:
        return (
            "No Bravo sent — that confirmation_token is not valid. It may already "
            "have been used (check whether the Bravo was sent before retrying). "
            "Call preview_bravo to get a fresh one."
        )

    # Everything below comes from the staged payload, never from the arguments.
    sending = result.payload or {}

    if await _recipient_name(sending["receiver_uid"], authorization) is None:
        return (
            "No Bravo sent — that person no longer exists in GoProfiles. Confirm "
            "the recipient with search_people and preview a new Bravo."
        )

    params = external_params(tool=_TOOL)
    try:
        response = await http_client.post(
            "/bravos.php",
            params=params,
            data={
                "bid": sending["bid"],
                "comment": sending["comment"],
                "receiver_uids[]": sending["receiver_uid"],
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
            f"Failed to send Bravo. {detail} Call preview_bravo again after "
            "confirming the recipient and badge with the user."
        )

    lines = [
        data.message.strip() or "Bravo sent successfully.",
        f"Badge:      {badge_name}",
        f"To:         {recipient_name}",
    ]
    if data.failed_count:
        lines.append(f"Failed:     {data.failed_count}")
    if data.scheduled:
        lines.append("Note:       This Bravo was scheduled for later delivery.")
    return "\n".join(lines)
