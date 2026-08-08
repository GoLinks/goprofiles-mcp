"""Bravo tools — badge catalog search and confirmed Bravo creation."""

from __future__ import annotations

from typing import Annotated, Any

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


def _points(value: int | None) -> int:
    """Normalize an optional point count to an int.

    Both tools normalize the same way so that omitting `points` on the send call
    when a non-zero amount was staged reads as 0, fails the confirm_args diff,
    and refuses the send — rather than silently spending what the user approved.
    """
    return 0 if value is None else int(value)


def _points_line(points: int) -> str:
    return str(points) if points else "none"


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
        str | None,
        Field(
            description=(
                "OMIT THIS to list every badge type in the workspace. That is what "
                "you want whenever the user has not said which badge they mean — "
                "never guess a search term to narrow the list for them, because "
                "the results are what you show them to choose from.\n"
                "Only pass a phrase when the user has described the kind of "
                "recognition they want ('going above and beyond', 'collaborat'). "
                "Matched as a case-insensitive substring of name and description."
            ),
        ),
    ] = None,
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
    """List the Bravo badge types that can be given in the user's GoProfiles
    workspace (https://www.goprofiles.io). Call with no arguments to list them all.

    Always call this tool in the current chat before naming any badge to the
    user — even if you already "know" a badge name like Team Player from earlier
    in this conversation, another chat, memory, or examples. Never tell the user
    which badges are available based on anything but this tool's output.

    When the user has not said which badge they want, call this with no search so
    they see the real choices. Do not invent a search term on their behalf: it
    silently hides badges, and presenting a filtered result as "the available
    badge" is wrong.

    Returns each badge type's name, description, and a numeric 'bid' for
    preview_bravo.

    After results return, ask the user for everything still missing in one
    message — which badge (unless they already named one) and what the message
    should say. Do not ask for these one turn at a time.

    The 'bid' is internal. Never show, read aloud, or otherwise expose it to the
    user; refer to badge types by name.

    Read-only. Requires bravos:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    badges = await _fetch_bravo_types(authorization, tool="search_bravo_types")
    query = (search or "").strip()

    scored: list[tuple[int, BravoTypeResult]] = []
    for badge in badges:
        score = _match_score(badge, query)
        if score is None:
            continue
        scored.append((score, badge))

    scored.sort(key=lambda item: (item[0], item[1].name.lower()))
    matches = [badge for _, badge in scored[:limit]]

    if not matches:
        if not query:
            return (
                "This workspace has no giveable bravo badge types. Tell the user "
                "there are no badges available to give, and do not attempt to "
                "preview or send a Bravo."
            )
        return (
            "No bravo badge types matched that search. Matching is substring-based "
            "against name and description — call this tool again with NO search to "
            "list every badge and let the user pick. Do not guess another search "
            "term on their behalf."
        )

    total = len(scored)
    names = [b.name or "Unknown" for b in matches]
    name_list = ", ".join(f"'{n}'" for n in names)
    shown = len(matches)
    if query:
        header = f"Bravo badge types ({shown} of {total} matched '{query}'):\n"
    else:
        header = f"Bravo badge types (all {total} available in this workspace):\n"
    entries = [f"[{i}]\n{_format_bravo_type(b)}" for i, b in enumerate(matches, 1)]

    # Ask for everything still missing in ONE turn. Asking for the badge and the
    # message in separate turns made a three-field action take four exchanges.
    if query:
        listing = (
            f"- These are only the badges matching '{query}', NOT the full list: "
            f"{name_list}. Say so if you present them, or call this tool again "
            "with no search to show everything."
        )
    elif shown < total:
        listing = (
            f"- Showing {shown} of {total} badges: {name_list}. Raise 'limit' to "
            "show the rest."
        )
    else:
        listing = f"- The badges available to give are: {name_list}."
    footer = (
        "\n\nNext step — ask for everything you still need in a SINGLE message:\n"
        + listing
        + "\n- If the user already named one of these badges, treat that as their "
        "choice and do NOT ask them to confirm it again.\n"
        "- Otherwise ask which one they want.\n"
        "- In the same message, unless they already gave you the message text, ask "
        "what the Bravo should say (and offer to draft it).\n"
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
                "The message to send with the Bravo (max 850 characters). Agree "
                "this with the user before calling — either they supply it or you "
                "draft it for their review. When asking them for it, call it "
                "simply 'the message'."
            ),
            min_length=1,
            max_length=850,
        ),
    ],
    points: Annotated[
        int | None,
        Field(
            description=(
                "Reward points to attach, ONLY when the user explicitly asked for "
                "a number ('give them 10 points'). Points are real spendable "
                "currency deducted from the sender's balance — never invent, "
                "suggest, or round an amount. Omit this whenever the user has not "
                "named a figure; omitted means no points, which is the normal case."
            ),
            ge=0,
        ),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Preview a Bravo before sending it. Sends nothing.

    Resolves the recipient and badge to their real names, checks both exist, and
    returns a preview of exactly what create_bravo will send. Nothing is sent
    and nothing is notified.

    Call this once you have the recipient, the badge, and the message. Gather the
    badge and the message together in a single question rather than one at a time
    — do not call this tool with a message you invented and have not shown the
    user. Do not ask how many points they want; leave points out unless they
    bring it up themselves.

    Then show the user the To / Badge / Points / Message from the result and wait
    for them to explicitly approve it. Only after that, call create_bravo.

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

    awarded = _points(points)
    stage(
        ctx,
        tool=_TOOL,
        # Executed on ids the model never resends, so the send call cannot be
        # redirected to a different person or badge.
        payload={
            "receiver_uid": receiver_uid,
            "bid": bid,
            "comment": comment,
            "points": awarded,
        },
        confirm_args={
            "recipient_name": recipient,
            "badge_name": badge.name,
            "comment": comment,
            # In confirm_args so the amount appears in the host's approval prompt
            # and is drift-checked — it is the only spendable field here.
            "points": awarded,
        },
    )

    return (
        "Bravo previewed — NOT sent.\n\n"
        f"To:      {recipient}\n"
        f"Badge:   {badge.name}\n"
        f"Points:  {_points_line(awarded)}\n"
        "Message:\n"
        f"{comment}\n\n"
        "NEXT STEP — show the user the To / Badge / Points / Message above and ask "
        "them to confirm. Do not call create_bravo until they explicitly say to "
        "send it. When they do, call create_bravo with recipient_name, badge_name, "
        "comment, and points copied exactly from this preview — including points "
        "when it is not 'none', or the send will be refused. create_bravo takes no "
        "uid, bid, or token; it sends the Bravo previewed here."
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
                "The message exactly as it appeared in the preview_bravo result "
                "('Message:'). Copy it verbatim — checked against the preview. Do "
                "not shorten, summarize, or reword it."
            ),
            min_length=1,
            max_length=850,
        ),
    ],
    points: Annotated[
        int | None,
        Field(
            description=(
                "The point count exactly as it appeared in the preview_bravo "
                "result ('Points:'). Copy it verbatim — checked against the "
                "preview, and shown to the user when they approve the send. Omit "
                "it only when the preview said 'none'; omitting it when the "
                "preview showed a number will refuse the send."
            ),
            ge=0,
        ),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Send a Bravo (peer recognition) that the user has already approved.

    ONLY call this after preview_bravo and after the user has explicitly said to
    send that preview. The user asking for a Bravo is not approval of its
    contents — they must approve the actual recipient, badge, and message first.

    Sends the recipient and badge resolved by preview_bravo, so this tool takes
    no uid or bid: the arguments here are the human-readable values the user
    approved, and they are checked against the preview rather than sent.

    When the preview showed a point count, pass it — points are deducted from the
    sender's balance, so omitting them is treated as a change to the approved
    Bravo and the send is refused.

    The sender is always the authenticated user, derived from the access token.

    Not read-only and not reversible — it notifies the recipient. Requires
    bravos:write and profiles:read scopes.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    awarded = _points(points)
    result = await claim(
        ctx,
        tool=_TOOL,
        confirm_args={
            "recipient_name": recipient_name,
            "badge_name": badge_name,
            "comment": comment.strip(),
            "points": awarded,
        },
        summary=(
            "Send this Bravo?\n\n"
            f"To: {recipient_name}\n"
            f"Badge: {badge_name}\n"
            f"Points: {_points_line(awarded)}\n\n"
            f"{comment.strip()}"
        ),
    )

    if result.status is ClaimStatus.DECLINED:
        return (
            "No Bravo sent — the user declined. Ask what they'd like to change. "
            "The preview is still valid if they only needed a moment; otherwise "
            "call preview_bravo again with the new details."
        )
    if result.status is ClaimStatus.DRIFTED:
        return (
            "No Bravo sent — the recipient, badge, message, or points do not match "
            "the preview. Copy recipient_name, badge_name, comment, and points "
            "verbatim from the preview_bravo result (points must be included when "
            "the preview showed a number), or call preview_bravo again if the user "
            "wants different content. The preview is still valid."
        )
    if result.status is ClaimStatus.EXPIRED:
        return (
            "No Bravo sent — the preview expired. Call preview_bravo again, then "
            "re-confirm with the user."
        )
    if not result.ok:
        return (
            "No Bravo sent — there is no Bravo waiting to be sent. It may already "
            "have been sent (check before retrying). Call preview_bravo first, "
            "show the user the preview, and send only after they approve it."
        )

    # Everything below comes from the staged payload, never from the arguments.
    sending = result.payload or {}

    if await _recipient_name(sending["receiver_uid"], authorization) is None:
        return (
            "No Bravo sent — that person no longer exists in GoProfiles. Confirm "
            "the recipient with search_people and preview a new Bravo."
        )

    sending_points = int(sending.get("points") or 0)
    # Form-encoded, not JSON: bid/comment/receiver_uids are read straight from
    # $_POST on the API side and a JSON body would drop them.
    body: dict[str, Any] = {
        "bid": sending["bid"],
        "comment": sending["comment"],
        "receiver_uids[]": sending["receiver_uid"],
    }
    # Omit rather than send 0 — an absent value stores NULL, which is what a
    # no-points Bravo looks like natively, and it keeps the ordinary case out of
    # the API's add-on-gated points path entirely.
    if sending_points > 0:
        body["points"] = sending_points

    params = external_params(tool=_TOOL)
    try:
        response = await http_client.post(
            "/bravos.php",
            params=params,
            data=body,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    try:
        raise_for_status(response, "/bravos.php")
    except (RuntimeError, ValueError):
        # The API rejects an over-cap amount with 400 and an unaffordable one
        # with 422, but masks the reason for most tenants — so name the likely
        # cause here rather than surfacing "An error occurred".
        if sending_points > 0:
            return (
                f"No Bravo sent — GoProfiles rejected it, and {sending_points} "
                "points may be the reason: the amount can exceed the company's "
                "per-Bravo limit, or the sender may not have enough points left. "
                "Tell the user it was not sent, ask for a smaller number, and "
                "call preview_bravo again."
            )
        raise

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
    if sending_points > 0:
        lines.append(f"Points:     {sending_points}")
    if data.failed_count:
        lines.append(f"Failed:     {data.failed_count}")
    if data.scheduled:
        lines.append("Note:       This Bravo was scheduled for later delivery.")
    return "\n".join(lines)
