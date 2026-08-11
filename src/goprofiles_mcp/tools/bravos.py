"""Bravo tools — badge catalog search, bravo history, and confirmed Bravo creation."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx
from fastmcp import Context
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    external_params,
    format_timestamp,
    get_authorization_header,
    http_client,
    raise_for_status,
)
from goprofiles_mcp.confirmations import ClaimStatus, claim, stage

# Only the create/confirm flow keys off this — it is the staging key, not a
# general "which tool am I" stamp. Read tools pass their own name to
# external_params.
_TOOL = "create_bravo"

# activity.php interpolates `days` straight into DATE_SUB(... INTERVAL $days DAY)
# and enforces no upper bound, so cap it here the way celebrations does.
_MAX_ACTIVITY_DAYS = 365

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BravoTypeResult(BaseModel):
    bid: int = 0
    name: str = ""
    description: str = ""


class BravoTypesResponse(BaseModel):
    results: list[BravoTypeResult] = []


class BravoActivityResult(BaseModel):
    """One already-sent bravo from the activity feed.

    The API row also carries both people's emails, four image URLs, the badge's
    bid, `mentions`, and `giver_is_manager`. Leaving them off the model is the
    allow-list: pydantic drops unmodeled keys, so they never reach the agent.
    Two are worth naming — `mentions` arrives as a GROUP_CONCAT string or as a
    list of dicts depending on whether the mentioned users resolved, and `bid`
    belongs to search_bravo_types, which is the only sanctioned source for it.
    """

    ubid: int = 0
    # Unix seconds, COALESCE(scheduled_time, created_at) — the effective send
    # time, which can sit slightly in the future for a scheduled bravo.
    created_at: int | None = None
    # The badge name. The API calls it 'name'; rendered as 'Bravo'.
    name: str = ""
    points: int | None = None
    comment: str | None = None
    receiver_first_name: str = ""
    receiver_last_name: str = ""
    receiver_username: str | None = None
    receiver_uid: int = 0
    receiver_department: str | None = None
    giver_first_name: str = ""
    giver_last_name: str = ""
    giver_username: str | None = None
    giver_uid: int = 0
    giver_department: str | None = None


class BravoActivityMetadata(BaseModel):
    limit: int = 0
    offset: int = 0
    total_results: int = 0
    count: int = 0


class BravoActivityResponse(BaseModel):
    metadata: BravoActivityMetadata = BravoActivityMetadata()
    results: list[BravoActivityResult] = []


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


# The API's own default timezone (config.inc sets date_default_timezone_set to
# this), used only when the caller gives a datetime with no offset. There is no
# OAuth-reachable endpoint that reveals the sender's own timezone, so a naive
# time cannot be resolved per-user.
_DEFAULT_TZ = ZoneInfo("America/Los_Angeles")

# No upper bound exists server-side, so cap it here. Catches a mistyped year and
# a millisecond epoch, both of which the API would otherwise accept silently.
_MAX_SCHEDULE_AHEAD = timedelta(days=365)


def _resolve_send_at(value: str | None) -> tuple[int, str | None]:
    """Turn an ISO 8601 string into an epoch. Returns (epoch, error_message).

    An epoch of 0 means "send immediately". Comparing resolved instants rather
    than raw strings lets two spellings of the same moment agree while a genuine
    change of time still fails the confirmation diff.
    """
    if value is None or not value.strip():
        return 0, None

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return 0, (
            f"'{value}' is not a valid date and time. Use ISO 8601, ideally with "
            "the user's UTC offset — for example 2026-08-12T09:00:00-07:00. Ask "
            "the user for the date and time they mean rather than guessing."
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_DEFAULT_TZ)

    now = datetime.now(UTC)
    if parsed <= now:
        return 0, (
            f"{_send_at_line(int(parsed.timestamp()))} is in the past. Bravos can "
            "only be scheduled for the future — ask the user for a later time, or "
            "omit the time to send it now."
        )
    if parsed - now > _MAX_SCHEDULE_AHEAD:
        return 0, (
            "That is more than a year away. Check the date with the user — a "
            "mistyped year is the usual cause — or omit the time to send now."
        )

    return int(parsed.timestamp()), None


def _relative(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"about {max(minutes, 1)} minute{'s' if minutes != 1 else ''} from now"
    hours = minutes // 60
    if hours < 48:
        return f"about {hours} hour{'s' if hours != 1 else ''} from now"
    return f"about {hours // 24} day{'s' if hours // 24 != 1 else ''} from now"


def _ago(seconds: float) -> str:
    """Past-tense counterpart to _relative, for bravos already sent."""
    minutes = int(seconds // 60)
    if minutes < 60:
        value = max(minutes, 1)
        return f"{value} minute{'s' if value != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _send_at_line(epoch: int) -> str:
    """Absolute time plus a relative form.

    The relative part is the load-bearing half: it carries no timezone, so a
    wrong offset is obvious to the user reading the preview.
    """
    if not epoch:
        return "immediately"
    when = datetime.fromtimestamp(epoch, tz=_DEFAULT_TZ)
    stamp = when.strftime("%a %d %b %Y, %H:%M %Z (UTC%z)")
    return f"{stamp} — {_relative(epoch - datetime.now(UTC).timestamp())}"


def _sent_line(ts: int | None) -> str:
    """Absolute UTC plus a relative clause, for a bravo in the activity feed.

    UTC rather than _DEFAULT_TZ: that constant exists to resolve naive *input*
    for scheduling, and every read-only tool in this server renders UTC.
    """
    if not ts:
        return "Unknown"
    delta = datetime.now(UTC).timestamp() - ts
    # A scheduled bravo is already flagged sent while its time is still ahead,
    # so don't render that as '0 minutes ago'.
    if delta < 0:
        return f"{format_timestamp(ts)} ({_relative(-delta)})"
    return f"{format_timestamp(ts)} ({_ago(delta)})"


def _person(first: str, last: str, username: str | None, department: str | None) -> str:
    name = f"{first} {last}".strip() or username or "Unknown"
    if username:
        name = f"{name} ({username})"
    # department is a LEFT JOIN, so absent is normal rather than an error.
    return f"{name} — {department}" if department else name


def _format_bravo_activity(b: BravoActivityResult) -> str:
    giver = _person(
        b.giver_first_name, b.giver_last_name, b.giver_username, b.giver_department
    )
    receiver = _person(
        b.receiver_first_name,
        b.receiver_last_name,
        b.receiver_username,
        b.receiver_department,
    )
    lines = [
        f"Bravo:     {b.name or 'Unknown'}",
        f"Given:     {_sent_line(b.created_at)}",
        f"From:      {giver}",
        f"To:        {receiver}",
    ]
    # Most bravos carry no points and no message; printing 'none' on every row
    # of a long feed is noise.
    if b.points:
        lines.append(f"Points:    {b.points}")
    if b.comment and b.comment.strip():
        lines.append(f"Comment:   {b.comment.strip()}")
    # uids are for follow-up get_profile calls only.
    lines.append(
        f"uid:       from {b.giver_uid} → to {b.receiver_uid}  "
        "(tool use only — do not show to the user)"
    )
    return "\n".join(lines)


# Department filters take name_id SLUGS, but no OAuth-reachable endpoint lists
# them — /d/api/departments refuses OAuth tokens outright. So the model passes
# the display names it already has from search_people/get_profile, and we derive
# the slug the same way the API does in Helpers::convertToNameID.
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def _department_slug(name: str) -> str:
    """'Customer Success' -> 'customer-success'; 'R&D' -> 'r-d'.

    Unescaping first is load-bearing: a department stored as 'R&amp;D' slugifies
    to 'r-d' server-side, but to 'r-amp-d' without it. Returns '' when nothing
    usable survives (an emoji- or non-Latin-only name), which Validate::nameID
    would reject with a 422.
    """
    return _NON_SLUG_CHARS.sub("-", html.unescape(name).strip().lower()).strip("-")


def _department_slugs(names: list[str] | None) -> tuple[list[str], list[str]]:
    """Slugify a department filter. Returns (slugs, names that slugified away)."""
    slugs: list[str] = []
    unusable: list[str] = []
    for name in names or []:
        slug = _department_slug(name)
        if not slug:
            unusable.append(name)
        elif slug not in slugs:
            slugs.append(slug)
    return slugs, unusable


def _filters_summary(
    days: int | None,
    person_name: str | None,
    giver_departments: list[str] | None,
    receiver_departments: list[str] | None,
) -> str:
    """Echo the filters that produced a result set, for the header and the
    empty-result prose — the caller cannot otherwise tell an empty feed from a
    filter that matched nothing."""
    parts = [f"last {days} day{'s' if days != 1 else ''}" if days else "all time"]
    if person_name:
        parts.append(f"'{person_name}' as giver or receiver")
    if giver_departments:
        parts.append("given by " + ", ".join(giver_departments))
    if receiver_departments:
        parts.append("received by " + ", ".join(receiver_departments))
    if len(parts) == 1:
        parts.append("everyone")
    return "; ".join(parts)


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
    return (
        f"{first} {last}".strip() or str(raw.get("username") or "").strip() or "Unknown"
    )


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


async def search_bravos(
    days: Annotated[
        int | None,
        Field(
            description=(
                "How many days back to look, 1–365 — a rolling window ending now, "
                "so days=1 is the last 24 hours. Omit it to search all time. This "
                "is the ONLY date filter: there is no calendar-date parameter and "
                "no way to look at a window that ended in the past, so pick a "
                "window wide enough to cover the dates the user asked about and "
                "read the exact 'Given' timestamp off each result."
            ),
            ge=1,
            le=_MAX_ACTIVITY_DAYS,
        ),
    ] = None,
    person_name: Annotated[
        str | None,
        Field(
            description=(
                "A person's name, e.g. 'Jane Roe' or just 'Roe'. Matched as a "
                "case-insensitive substring of 'first last', so a misspelling "
                "returns nothing.\n"
                "NOT directional: this returns bravos the person GAVE as well as "
                "bravos they RECEIVED, and the API offers no way to ask for only "
                "one direction. If the user asked about only one, read the 'From' "
                "and 'To' lines of each result to tell them apart, and say which "
                "you are reporting."
            )
        ),
    ] = None,
    giver_departments: Annotated[
        list[str] | None,
        Field(
            description=(
                "Only bravos GIVEN by someone in these departments. Pass department "
                "display names exactly as they appear in search_people or "
                "get_profile results, e.g. ['Engineering', 'Customer Success'] — do "
                "not invent department names and do not convert them to slugs."
            )
        ),
    ] = None,
    receiver_departments: Annotated[
        list[str] | None,
        Field(
            description=(
                "Only bravos RECEIVED by someone in these departments, same format "
                "as 'giver_departments'. Passing both narrows to bravos matching "
                "BOTH at once (e.g. Engineering thanking Sales), not either one."
            )
        ),
    ] = None,
    limit: Annotated[
        int, Field(description="Number of bravos to return (1–100).", ge=1, le=100)
    ] = 20,
    offset: Annotated[int, Field(description="Pagination offset (0-based).", ge=0)] = 0,
    ctx: Context | None = None,
) -> str:
    """Search the history of Bravos already given in the user's GoProfiles
    workspace (https://www.goprofiles.io) — peer recognition that has been sent.

    This is the recognition FEED, not the badge catalog and not a way to send
    anything: use search_bravo_types to see which badges exist, and
    preview_bravo to send one. Use this tool to answer questions about what
    already happened — who was recognized recently, what someone has been
    thanked for, which teams are giving recognition.

    Each result gives the badge, when it was given, who gave it, who received
    it, their departments, any points, and the message. Results are always
    newest first; there is no sort option.

    Filters combine (they narrow each other, never widen). Date filtering is a
    relative past-only day window, not calendar dates. The person filter is not
    directional — it matches the giver or the receiver — so check 'From' and 'To'
    before telling the user someone gave rather than received a bravo.

    Each entry carries numeric 'uid' values for follow-up get_profile calls.
    Never show, read aloud, or otherwise expose them; refer to people by name,
    username, or department instead.

    Read-only. Requires bravos:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    # Normalize once, so the filter that gets sent and the filter that gets
    # echoed back in the header are the same string.
    person = (person_name or "").strip() or None

    giver_slugs, giver_unusable = _department_slugs(giver_departments)
    receiver_slugs, receiver_unusable = _department_slugs(receiver_departments)
    # A filter whose every name slugified away would silently vanish from the
    # query and return an unfiltered feed that reads like an answer. Refuse
    # instead of answering the wrong question.
    for label, requested, slugs in (
        ("giver_departments", giver_departments, giver_slugs),
        ("receiver_departments", receiver_departments, receiver_slugs),
    ):
        if requested and not slugs:
            return (
                f"No bravos fetched — none of the {label} names contain letters or "
                "digits, so that filter could not be applied and the results would "
                "have been unfiltered. Pass department names as they appear in "
                "search_people results."
            )

    # Echo only the departments that survived slugification; the footer names the
    # dropped ones separately rather than implying they were applied.
    giver_used = [n for n in giver_departments or [] if n not in giver_unusable]
    receiver_used = [
        n for n in receiver_departments or [] if n not in receiver_unusable
    ]

    # filter=bravos is required, not optional: without it the feed mixes in
    # achievement rows AND the department filters are ignored outright.
    raw_params: dict = {"filter": "bravos", "limit": limit, "offset": offset}
    if days is not None:
        raw_params["days"] = days
    if person:
        raw_params["search"] = person
    # The '[]' suffix is what makes PHP read these as arrays; httpx expands a
    # list value into repeated giver_departments[]=… params.
    if giver_slugs:
        raw_params["giver_departments[]"] = giver_slugs
    if receiver_slugs:
        raw_params["receiver_departments[]"] = receiver_slugs

    params = external_params(raw_params, tool="search_bravos")

    try:
        response = await http_client.get(
            "/activity.php",
            params=params,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, "/activity.php")

    data = BravoActivityResponse.model_validate(response.json())
    m = data.metadata
    summary = _filters_summary(days, person, giver_used, receiver_used)

    # An empty feed is a normal outcome, never an error — name the filters that
    # produced it, since the caller cannot otherwise tell "no bravos exist" from
    # "one filter matched nothing".
    if not data.results:
        if m.total_results and offset >= m.total_results:
            return (
                f"No more bravos at offset {offset} — this search has "
                f"{m.total_results} result(s) in total. Lower 'offset' to page back "
                "through them."
            )
        remedies = ["widen the window with 'days', or omit it to search all time"]
        if person:
            remedies.append(
                f"'{person}' is matched as a substring of 'first last', so a "
                "misspelling returns nothing — try the last name on its own"
            )
        if giver_used or receiver_used:
            remedies.append(
                "check the department names match those in search_people results "
                "exactly, and note that passing both giver_departments and "
                "receiver_departments requires BOTH to match"
            )
        return (
            f"No bravos found for: {summary}. Tell the user nothing matched, then "
            "adjust one filter at a time — " + "; ".join(remedies) + "."
        )

    header = (
        f"Bravos ({m.count} of {m.total_results} total, offset {m.offset}) — "
        f"{summary}. Newest first:"
    )
    entries = [
        f"[{i}]\n{_format_bravo_activity(b)}"
        for i, b in enumerate(data.results, start=offset + 1)
    ]

    footer = ""
    dropped = giver_unusable + receiver_unusable
    if dropped:
        footer = (
            "\n\nNote: ignored these department name(s), which contain no letters "
            "or digits: "
            + ", ".join(f"'{n}'" for n in dropped)
            + ". The results are NOT filtered by them."
        )

    return header + "\n\n" + "\n\n".join(entries) + footer


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
    preview_bravo. This is the catalog of what CAN be given — for bravos that
    have already been given, use search_bravos.

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
    send_at: Annotated[
        str | None,
        Field(
            description=(
                "When to send it, ONLY when the user asked for a specific time "
                "('send it Monday morning'). ISO 8601 — include the user's UTC "
                "offset whenever you know it, e.g. '2026-08-12T09:00:00-07:00'; "
                "without an offset the time is read as US Pacific. Never invent a "
                "time. Omit this to send immediately, which is the normal case. "
                "Once scheduled, it cannot be cancelled or rescheduled from this "
                "chat — the user can still edit it later in GoProfiles."
            ),
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
    when, when_error = _resolve_send_at(send_at)
    if when_error is not None:
        return f"No preview — {when_error}"

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
            "send_at": when,
        },
        confirm_args={
            "recipient_name": recipient,
            "badge_name": badge.name,
            "comment": comment,
            # Both of these are in confirm_args so they appear in the host's
            # approval prompt and are drift-checked: one is spendable, the other
            # schedules a delivery that cannot be changed from this chat.
            "points": awarded,
            "send_at": when,
        },
    )

    caveats = ""
    if when:
        caveats = (
            "\nOnce scheduled, it cannot be cancelled or rescheduled from this "
            "chat (the user can still edit it later in GoProfiles) — tell them "
            "that before they confirm."
        )
        if awarded:
            caveats += (
                f" The {awarded} point(s) leave their balance as soon as it is "
                "scheduled; the recipient receives them when it actually sends."
            )
        caveats += "\n"

    return (
        "Bravo previewed — NOT sent.\n\n"
        f"To:      {recipient}\n"
        f"Badge:   {badge.name}\n"
        f"Points:  {_points_line(awarded)}\n"
        f"Sends:   {_send_at_line(when)}\n"
        + (
            "         Delivered on the next hourly run at or after that time.\n"
            if when
            else ""
        )
        + "Message:\n"
        f"{comment}\n"
        f"{caveats}\n"
        "NEXT STEP — show the user the To / Badge / Points / Sends / Message above "
        "and ask them to confirm. Do not call create_bravo until they explicitly "
        "say to send it. When they do, call create_bravo with recipient_name, "
        "badge_name, comment, points, and send_at copied exactly from this preview "
        "— including points and send_at when they are set, or the send will be "
        "refused. create_bravo takes no uid, bid, or token; it sends the Bravo "
        "previewed here."
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
    send_at: Annotated[
        str | None,
        Field(
            description=(
                "The send time from the preview_bravo result ('Sends:'), as the "
                "same ISO 8601 value you passed to preview_bravo. Omit it only "
                "when the preview said 'immediately'; omitting it when the preview "
                "showed a time will refuse the send."
            ),
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

    When the preview showed a point count or a send time, pass them — points are
    deducted from the sender's balance and a send time schedules delivery that
    cannot be changed from this chat (only later in GoProfiles), so omitting
    either is treated as a change to the approved Bravo and the send is refused.

    The sender is always the authenticated user, derived from the access token.

    Not read-only and not reversible — it notifies the recipient. Requires
    bravos:write and profiles:read scopes.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    awarded = _points(points)
    when, when_error = _resolve_send_at(send_at)
    if when_error is not None:
        return f"No Bravo sent — {when_error}"

    result = await claim(
        ctx,
        tool=_TOOL,
        confirm_args={
            "recipient_name": recipient_name,
            "badge_name": badge_name,
            "comment": comment.strip(),
            "points": awarded,
            "send_at": when,
        },
        summary=(
            ("Schedule this Bravo?" if when else "Send this Bravo?") + "\n\n"
            f"To: {recipient_name}\n"
            f"Badge: {badge_name}\n"
            f"Points: {_points_line(awarded)}\n"
            f"Sends: {_send_at_line(when)}\n"
            + (
                "Cannot be cancelled or rescheduled from this chat once "
                "scheduled (editable later in GoProfiles).\n"
                if when
                else ""
            )
            + f"\n{comment.strip()}"
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
            "No Bravo sent — the recipient, badge, message, points, or send time "
            "do not match the preview. Copy recipient_name, badge_name, comment, "
            "points, and send_at verbatim from the preview_bravo result (points "
            "and send_at must be included whenever the preview showed them), or "
            "call preview_bravo again if the user wants different content. The "
            "preview is still valid."
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
    # The API takes a numeric epoch and rejects date strings outright.
    sending_when = int(sending.get("send_at") or 0)
    if sending_when > 0:
        body["scheduled_time"] = sending_when

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
        reasons = []
        if sending_points > 0:
            reasons.append(
                f"the {sending_points} points may exceed the company's per-Bravo "
                "limit or the sender's remaining balance"
            )
        if sending_when > 0:
            reasons.append("the scheduled time may no longer be in the future")
        if reasons:
            return (
                "No Bravo sent — GoProfiles rejected it, and "
                + " or ".join(reasons)
                + ". Tell the user it was not sent, ask them what to change, and "
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

    default = "Bravo scheduled." if data.scheduled else "Bravo sent successfully."
    lines = [
        data.message.strip() or default,
        f"Badge:      {badge_name}",
        f"To:         {recipient_name}",
    ]
    if sending_points > 0:
        lines.append(f"Points:     {sending_points}")
    if data.failed_count:
        lines.append(f"Failed:     {data.failed_count}")
    if data.scheduled:
        lines.append(f"Sends:      {_send_at_line(sending_when)}")
        lines.append(
            "Note:       Delivered on the next hourly run at or after that time. "
            "Cannot be cancelled or rescheduled from this chat — edit it in "
            "GoProfiles if needed."
        )
    return "\n".join(lines)
