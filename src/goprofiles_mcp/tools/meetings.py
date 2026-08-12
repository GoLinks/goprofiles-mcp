"""Meeting tools — confirmed calendar invite creation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx
from fastmcp import Context
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    api_get,
    external_params,
    get_authorization_header,
    http_client,
    raise_for_status,
)
from goprofiles_mcp.confirmations import ClaimStatus, claim, stage

_TOOL = "schedule_meeting"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The two calendar integrations GoProfiles supports. A workspace enables at most
# one; the other answers 403, which is how we tell which is in play. Each read
# path detects the provider; the invite is POSTed to `<path>/schedule_meeting.php`.
_PROVIDERS = (
    ("/google-calendar", "Google Calendar"),
    ("/outlook-calendar", "Outlook Calendar"),
)

# The API's own ceiling — it rejects anything longer outright. Mirrored in the
# duration Field so an over-long meeting fails before a round trip.
_MAX_DURATION_MINUTES = 1440

# The API's own default timezone (config.inc sets date_default_timezone_set to
# this), used only when the caller gives a datetime with no offset. There is no
# OAuth-reachable endpoint that reveals the organizing user's own timezone, so a
# naive time cannot be resolved per-user.
_DEFAULT_TZ = ZoneInfo("America/Los_Angeles")

# No upper bound exists server-side, so cap it here. Catches a mistyped year and
# a millisecond epoch, both of which the API would otherwise accept silently.
_MAX_SCHEDULE_AHEAD = timedelta(days=365)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ScheduleMeetingResponse(BaseModel):
    # Google returns only `link`. Outlook also sends `meeting_link`, which is
    # null when the event carries no online meeting.
    link: str = ""
    meeting_link: str | None = None


class ProviderProbe(BaseModel):
    """Whether one calendar integration is the one this workspace runs."""

    label: str = ""
    invite_path: str = ""
    # ok = answered; on = enabled but this read failed; off = feature disabled
    outcome: str = "off"


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def _invite_path(read_path: str) -> str:
    return f"{read_path}/schedule_meeting.php"


async def _probe_provider(
    read_path: str, label: str, uid: int, authorization: str
) -> ProviderProbe:
    """Ask one calendar integration whether it is enabled for this workspace.

    Only a 403 is evidence. The company feature gate runs before anything else
    on both endpoints, so a 403 means this provider is off. Every other outcome
    — a 404 for an unlinked mailbox, a 500 from the provider — already got past
    that gate, which is all this needs to establish.

    Nothing here raises: a workspace runs one provider, so the other one's 403
    is the expected case rather than an error worth aborting the preview for. A
    bad or unscoped token has already failed the /users.php call made before
    this one, so a 403 here cannot be an auth problem in disguise.
    """
    probe = ProviderProbe(label=label, invite_path=_invite_path(read_path))
    params = external_params({"uid": uid, "events": 1}, tool="preview_meeting")

    try:
        await api_get(read_path, params, authorization)
    except PermissionError:
        return probe
    except (LookupError, TimeoutError, ConnectionError, RuntimeError, ValueError):
        probe.outcome = "on"
        return probe

    probe.outcome = "ok"
    return probe


def _pick_provider(probes: list[ProviderProbe]) -> ProviderProbe | None:
    """The provider this workspace runs, or None when neither is enabled.

    Strongest signal first: a provider that answered, then one that is enabled
    but whose read failed. Google before Outlook within each tier, matching
    get_availability's ordering.
    """
    for outcome in ("ok", "on"):
        for probe in probes:
            if probe.outcome == outcome:
                return probe
    return None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _relative(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"about {max(minutes, 1)} minute{'s' if minutes != 1 else ''} from now"
    hours = minutes // 60
    if hours < 48:
        return f"about {hours} hour{'s' if hours != 1 else ''} from now"
    days = hours // 24
    return f"about {days} day{'s' if days != 1 else ''} from now"


def _resolve_start_at(value: str) -> tuple[int, tzinfo | None, str | None]:
    """Turn an ISO 8601 string into (epoch, display zone, error_message).

    The zone comes back so the preview and the host's approval prompt can render
    the time in the offset the caller supplied. Comparing resolved instants
    rather than raw strings lets two spellings of the same moment agree while a
    genuine change of time still fails the confirmation diff.
    """
    text = value.strip()
    if not text:
        return (
            0,
            None,
            (
                "no start time was given. Ask the user for the day and time they "
                "want, and pass it as ISO 8601 with their UTC offset — for "
                "example 2026-08-13T14:00:00-07:00."
            ),
        )

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return (
            0,
            None,
            (
                f"'{value}' is not a valid date and time. Use ISO 8601, ideally "
                "with the user's UTC offset — for example "
                "2026-08-13T14:00:00-07:00. Ask the user for the date and time "
                "they mean rather than guessing."
            ),
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_DEFAULT_TZ)

    now = datetime.now(UTC)
    if parsed <= now:
        return (
            0,
            None,
            (
                f"{parsed.strftime('%a %d %b %Y, %H:%M (UTC%z)')} is in the past. "
                "A meeting can only be scheduled for the future — ask the user "
                "for a later time."
            ),
        )
    if parsed - now > _MAX_SCHEDULE_AHEAD:
        return (
            0,
            None,
            (
                "that is more than a year away. Check the date with the user — a "
                "mistyped year is the usual cause."
            ),
        )

    return int(parsed.timestamp()), parsed.tzinfo, None


def _when_line(epoch: int, minutes: int, zone: tzinfo | None) -> str:
    """Absolute start–end plus a relative form.

    The relative part is the load-bearing half: it carries no timezone, so a
    wrong offset is obvious to the user reading the preview. The absolute time is
    only ever rendered in the offset the caller supplied, because this server
    cannot learn the organizing user's own timezone.
    """
    display = zone or _DEFAULT_TZ
    start = datetime.fromtimestamp(epoch, tz=display)
    # Derived from the epoch rather than added to the wall clock, so a meeting
    # that spans a DST transition still ends at the right local time.
    end = datetime.fromtimestamp(epoch + minutes * 60, tz=display)
    # A long meeting can end on another day; showing a bare '20:36–20:36' for a
    # full 24 hours reads as a zero-length slot.
    end_format = "%H:%M" if end.date() == start.date() else "%a %d %b %Y, %H:%M"
    relative = _relative(epoch - datetime.now(UTC).timestamp())
    return (
        f"{start.strftime('%a %d %b %Y, %H:%M')}–{end.strftime(end_format)} "
        f"(UTC{start.strftime('%z')}) — {relative}"
    )


def _duration_line(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts) or "0 minutes"


# ---------------------------------------------------------------------------
# People helpers
# ---------------------------------------------------------------------------


async def _attendee_name(uid: int, authorization: str, *, tool: str) -> str | None:
    """Confirm a uid exists and return a display name, or None if it doesn't.

    Same two-layer check get_profile uses: /users.php 404s via LookupError, but
    it can also answer 200 with a body that carries no uid.
    """
    params = external_params({"uid": uid}, tool=tool)

    try:
        response = await api_get("/users.php", params, authorization)
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


def _rejection(status: int) -> str:
    """Name the likely cause of a rejected invite.

    The endpoint reuses status codes across unrelated failures — 404 covers a
    start time that has passed as well as a missing attendee, and a disconnected
    organizer calendar arrives as a 500 — so "Not found" or a raw 500 would be
    actively misleading here.
    """
    if status == 404:
        causes = (
            "the start time may have passed, the attendee may no longer exist, "
            "or they may have no mailbox in the workspace's calendar tenant"
        )
    elif status == 400:
        causes = (
            "the attendee may be the signed-in user themselves — the organizer is "
            "always the signed-in user, so the attendee has to be someone else — "
            "or the title or description may be too long"
        )
    else:
        causes = (
            "the organizer's calendar may not be connected to GoProfiles, or the "
            "calendar provider rejected the event"
        )
    return (
        f"No invite created — GoProfiles rejected it, and {causes}. Tell the user "
        "no invite was created and nobody was notified, ask them what to change, "
        "and call preview_meeting again."
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def preview_meeting(
    uid: Annotated[
        int,
        Field(
            description=(
                "Numeric user id of the person to meet with, from search_people "
                "in THIS conversation. Pass the uid only — never show it to the "
                "user. The organizer is always the authenticated user; there is "
                "no organizer parameter and you must not invent one."
            ),
            ge=1,
        ),
    ],
    starts_at: Annotated[
        str,
        Field(
            description=(
                "When the meeting starts, as ISO 8601 — include the user's UTC "
                "offset whenever you know it, e.g. '2026-08-13T14:00:00-07:00'; "
                "without an offset the time is read as US Pacific, which is often "
                "wrong. Never invent a time. Nothing here can look up whether "
                "someone is free on a future date, so the day and time have to "
                "come from the user."
            ),
            min_length=1,
        ),
    ],
    duration_minutes: Annotated[
        int,
        Field(
            description=(
                "How long the meeting runs, in minutes (1–1440; 1440 is a full "
                "day and the hard maximum the API allows). Use the length the "
                "user asked for. If they only said something like 'a quick "
                "chat', 30 is a reasonable default — but say which length you "
                "used when you show them the preview."
            ),
            ge=1,
            le=_MAX_DURATION_MINUTES,
        ),
    ],
    title: Annotated[
        str,
        Field(
            description=(
                "The meeting title, as it will appear on both calendars (max 100 "
                "characters). Agree it with the user before calling — either they "
                "supply it or you draft it for their review."
            ),
            min_length=1,
            max_length=100,
        ),
    ],
    description: Annotated[
        str,
        Field(
            description=(
                "The invite body, shown to both people in the calendar event (max "
                "8192 characters; simple HTML is allowed). Required by the API. Do "
                "not invent text the user has not seen — draft it so it appears in "
                "the preview, or ask them what the invite should say. A one-line "
                "agenda is fine."
            ),
            min_length=1,
            max_length=8192,
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Preview a calendar invite before creating it. Creates nothing.

    Resolves the attendee to their real name, checks they exist, works out which
    calendar the workspace uses (Google or Outlook), and returns a preview of
    exactly what schedule_meeting will create. No event is created, no calendar
    is touched, and nobody is notified.

    The start time has to come from the user. This server cannot check whether
    someone is free on a future date — get_availability only reports what is left
    of that person's TODAY, so it can sanity-check a slot later today and nothing
    beyond that. Never present a future slot as free, and never pick a time on
    the user's behalf: ask them for the day and time, and put their UTC offset in
    starts_at.

    Ask for everything still missing in ONE message — the time, the length, the
    title, and what the invite should say. Do not ask for these one turn at a
    time, and do not call this tool with a title or description you invented and
    have not shown the user.

    Then show the user the With / When / Duration / Title / Description from the
    result and wait for them to explicitly approve it. Only after that, call
    schedule_meeting.

    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    title = title.strip()
    description = description.strip()
    if not title:
        return (
            "No preview — the title is empty. Ask the user what the meeting "
            "should be called, or offer to draft a title for their review."
        )
    if not description:
        return (
            "No preview — the invite body is empty and the API requires one. Ask "
            "the user what the invite should say, or offer to draft it."
        )

    epoch, zone, time_error = _resolve_start_at(starts_at)
    if time_error is not None:
        return f"No preview — {time_error}"

    attendee = await _attendee_name(uid, authorization, tool="preview_meeting")
    if attendee is None:
        return (
            "No preview — no person found with that uid. Confirm the attendee "
            "with search_people and try again with the uid from its results."
        )

    # Both providers are probed together: only one is enabled per workspace, and
    # which one is not knowable up front without a second round trip.
    probes = await asyncio.gather(
        *(
            _probe_provider(read_path, label, uid, authorization)
            for read_path, label in _PROVIDERS
        )
    )
    provider = _pick_provider(list(probes))
    if provider is None:
        return (
            "No preview — this GoProfiles workspace has no calendar integration "
            "connected, so an invite cannot be created from here. Tell the user "
            "that and suggest they set the meeting up in their calendar directly."
        )

    stage(
        ctx,
        tool=_TOOL,
        # Executed on the uid and provider resolved here, neither of which the
        # model resends, so the send call cannot be redirected to a different
        # person or calendar. There is deliberately no organizer in the payload:
        # the API derives it from the access token.
        payload={
            "uid_to_meet": uid,
            "starting_time": epoch,
            "meeting_duration_min": duration_minutes,
            "title": title,
            "description": description,
            "invite_path": provider.invite_path,
            "provider": provider.label,
        },
        confirm_args={
            "attendee_name": attendee,
            # The resolved instant, not the string: two spellings of the same
            # moment agree, while a genuine change of time fails the diff.
            "starts_at": epoch,
            "duration_minutes": duration_minutes,
            "title": title,
            "description": description,
        },
    )

    return (
        "Meeting previewed — NO invite created and nobody notified.\n\n"
        f"With:        {attendee}\n"
        f"When:        {_when_line(epoch, duration_minutes, zone)}\n"
        f"Duration:    {_duration_line(duration_minutes)}\n"
        f"Calendar:    {provider.label}\n"
        f"Title:       {title}\n"
        "Description:\n"
        f"{description}\n\n"
        "Check the When line against what the user actually asked for. If the "
        "relative time ('about N hours from now') looks wrong, the UTC offset was "
        "wrong — ask them for their timezone and preview again.\n\n"
        "NEXT STEP — show the user the With / When / Duration / Title / "
        "Description above and ask them to confirm. Do not call schedule_meeting "
        "until they explicitly say to send the invite. When they do, call "
        "schedule_meeting with attendee_name, starts_at, duration_minutes, title, "
        "and description copied exactly from this preview — starts_at must be the "
        "same value you passed here, or the invite will be refused. "
        "schedule_meeting takes no uid and no organizer: it creates the meeting "
        "previewed here, organized by the signed-in user."
    )


async def schedule_meeting(
    attendee_name: Annotated[
        str,
        Field(
            description=(
                "The attendee's name exactly as it appeared in the "
                "preview_meeting result ('With:'). Copy it verbatim. This is "
                "shown to the user when they approve the invite, and is checked "
                "against the preview."
            ),
            min_length=1,
        ),
    ],
    starts_at: Annotated[
        str,
        Field(
            description=(
                "The start time from the preview_meeting result, as the same ISO "
                "8601 value you passed to preview_meeting. Copy it verbatim — "
                "checked against the preview."
            ),
            min_length=1,
        ),
    ],
    duration_minutes: Annotated[
        int,
        Field(
            description=(
                "The length in minutes exactly as it appeared in the "
                "preview_meeting result ('Duration:'). Copy it verbatim — checked "
                "against the preview."
            ),
            ge=1,
            le=_MAX_DURATION_MINUTES,
        ),
    ],
    title: Annotated[
        str,
        Field(
            description=(
                "The title exactly as it appeared in the preview_meeting result "
                "('Title:'). Copy it verbatim — checked against the preview."
            ),
            min_length=1,
            max_length=100,
        ),
    ],
    description: Annotated[
        str,
        Field(
            description=(
                "The invite body exactly as it appeared in the preview_meeting "
                "result ('Description:'). Copy it verbatim — checked against the "
                "preview. Do not shorten, summarize, or reword it."
            ),
            min_length=1,
            max_length=8192,
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Create a calendar invite that the user has already approved.

    ONLY call this after preview_meeting and after the user has explicitly said
    to send that invite. The user asking for a meeting is not approval of its
    contents — they must approve the actual attendee, time, length, title, and
    invite body first.

    Creates the meeting resolved by preview_meeting, so this tool takes no uid:
    the arguments here are the human-readable values the user approved, and they
    are checked against the preview rather than sent.

    The organizer is always the authenticated user, derived from the access
    token. There is no way to schedule on someone else's behalf, and no
    parameter that could point the invite at a different person or calendar.

    Not read-only and not reversible — it creates a real event on both people's
    calendars and emails the invite. Requires profiles:write and profiles:read
    scopes.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    epoch, zone, time_error = _resolve_start_at(starts_at)
    if time_error is not None:
        return (
            f"No invite created — {time_error} If the start time simply passed "
            "while the user was deciding, call preview_meeting again with a new "
            "time and re-confirm with them."
        )

    title = title.strip()
    description = description.strip()

    result = await claim(
        ctx,
        tool=_TOOL,
        confirm_args={
            "attendee_name": attendee_name,
            "starts_at": epoch,
            "duration_minutes": duration_minutes,
            "title": title,
            "description": description,
        },
        summary=(
            "Send this calendar invite?\n\n"
            f"With: {attendee_name}\n"
            f"When: {_when_line(epoch, duration_minutes, zone)}\n"
            f"Duration: {_duration_line(duration_minutes)}\n"
            f"Title: {title}\n"
            "Creates a real event on both calendars and emails the invite.\n"
            f"\n{description}"
        ),
    )

    if result.status is ClaimStatus.DECLINED:
        return (
            "No invite created — the user declined. Ask what they'd like to "
            "change. The preview is still valid if they only needed a moment; "
            "otherwise call preview_meeting again with the new details."
        )
    if result.status is ClaimStatus.DRIFTED:
        return (
            "No invite created — the attendee, start time, duration, title, or "
            "invite body do not match the preview. Copy attendee_name, "
            "starts_at, duration_minutes, title, and description verbatim from "
            "the preview_meeting result, or call preview_meeting again if the "
            "user wants something different. The preview is still valid."
        )
    if result.status is ClaimStatus.EXPIRED:
        return (
            "No invite created — the preview expired. Call preview_meeting "
            "again, then re-confirm with the user."
        )
    if not result.ok:
        return (
            "No invite created — there is no meeting waiting to be scheduled. It "
            "may already have been created (check before retrying). Call "
            "preview_meeting first, show the user the preview, and schedule only "
            "after they approve it."
        )

    # Everything below comes from the staged payload, never from the arguments.
    sending = result.payload or {}

    if await _attendee_name(sending["uid_to_meet"], authorization, tool=_TOOL) is None:
        return (
            "No invite created — that person no longer exists in GoProfiles. "
            "Confirm the attendee with search_people and preview a new meeting."
        )

    # Form-encoded, not JSON: these are read through getRequestParam, which
    # merges $_GET and $_POST. `organizer_uid` is deliberately absent — the
    # endpoint forces the organizer to the authenticated session user for
    # external callers, and sending one would be a client-supplied organizer.
    body: dict[str, Any] = {
        "uid_to_meet": sending["uid_to_meet"],
        "starting_time": sending["starting_time"],
        "meeting_duration_min": sending["meeting_duration_min"],
        "title": sending["title"],
        "description": sending["description"],
    }

    path = sending["invite_path"]
    params = external_params(tool=_TOOL)
    try:
        response = await http_client.post(
            path,
            params=params,
            data=body,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    # Explained failures first; 401, 403, and 429 keep their normal meanings and
    # are raised by raise_for_status. A 403 here means the workspace's calendar
    # feature was turned off — that gate runs before any event is created, so
    # nothing was scheduled.
    if response.status_code in (400, 404, 500):
        return _rejection(response.status_code)
    raise_for_status(response, path)

    data = ScheduleMeetingResponse.model_validate(response.json())

    lines = [
        "Calendar invite created and sent.",
        f"With:      {attendee_name}",
        f"When:      {_when_line(epoch, duration_minutes, zone)}",
        f"Duration:  {_duration_line(duration_minutes)}",
        f"Calendar:  {sending.get('provider') or 'Unknown'}",
        f"Title:     {title}",
    ]
    if data.link:
        lines.append(f"Event:     {data.link}")
    if data.meeting_link:
        lines.append(f"Join:      {data.meeting_link}")
    if not data.link:
        lines.append(
            "Note:      GoProfiles returned no event link. Tell the user to check "
            "their calendar rather than presenting the meeting as confirmed."
        )
    return "\n".join(lines)
