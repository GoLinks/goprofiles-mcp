"""Meeting tools — confirmed calendar invite creation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from goprofiles_mcp.tools.availability import (
    CalendarEvent,
    CalendarResponse,
    as_event,
)

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
    # Both nullable: this is parsed only after the invite already exists and the
    # staged write is spent, so a validation error here would report failure for
    # a meeting that was created — and a retry would book a second one.
    link: str | None = None
    meeting_link: str | None = None


class Attendee(BaseModel):
    name: str = "Unknown"
    # IANA name off their profile. Absent for plenty of real people.
    timezone: str | None = None
    title: str | None = None
    city: str | None = None
    state: str | None = None
    # Backs the default description's profile link. Real accounts always have
    # one; empty is only a defensive fallback.
    username: str = ""


class ProviderProbe(BaseModel):
    """Whether one calendar integration is the one this workspace runs.

    Carries the attendee's calendar payload too: detecting the provider already
    costs a read of it, so the conflict advisory below is free.
    """

    label: str = ""
    invite_path: str = ""
    # ok = answered; on = enabled but this read failed; off = feature disabled
    outcome: str = "off"
    # Populated only when outcome is ok. `linked` is False when the person has
    # not connected their own calendar, which the provider reports in the body
    # of a 200 rather than as a status code.
    linked: bool = True
    events: list[CalendarEvent] = []
    ooo: CalendarEvent | None = None


class ConflictCheck(BaseModel):
    """The advisory verdict on whether the attendee is free for the slot.

    `state` is deliberately three-valued. "unchecked" must never be rendered as
    "free": the events feed only covers the rest of the attendee's current day,
    so most future slots cannot be checked at all, and reading silence as
    clearance is the failure mode this whole struct exists to prevent.
    """

    # conflict | clear | unchecked
    state: str = "unchecked"
    detail: str = ""


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

    `events` and `ooo` are requested because the detection round trip has to be
    made regardless, and they are what powers the conflict advisory. `ooo` is the
    more useful of the two: the events feed stops at the end of the attendee's
    current day, while the provider looks two months ahead for time off.

    Nothing here raises: a workspace runs one provider, so the other one's 403
    is the expected case rather than an error worth aborting the preview for. A
    bad or unscoped token has already failed the /users.php call made before
    this one, so a 403 here cannot be an auth problem in disguise.
    """
    probe = ProviderProbe(label=label, invite_path=_invite_path(read_path))
    params = external_params(
        {"uid": uid, "events": 1, "ooo": 1}, tool="preview_meeting"
    )

    try:
        response = await api_get(read_path, params, authorization)
    except PermissionError:
        return probe
    except (LookupError, TimeoutError, ConnectionError, RuntimeError, ValueError):
        probe.outcome = "on"
        return probe

    probe.outcome = "ok"

    data = CalendarResponse.model_validate(response.json())
    # Google reports a person who has not linked their calendar in the body of a
    # 200. Outlook never sends `status`, so only 'failure' is a signal.
    if data.status == "failure":
        probe.linked = False
        return probe

    probe.events = data.events
    probe.ooo = as_event(data.ooo)
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


def _optional(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


async def _attendee(uid: int, authorization: str, *, tool: str) -> Attendee | None:
    """Confirm a uid exists and return their profile, or None if they don't.

    Same two-layer check get_profile uses: /users.php 404s via LookupError, but
    it can also answer 200 with a body that carries no uid.

    Timezone, title, city, state, and username all come from this same payload
    at no extra cost. Timezone is what places the end of the attendee's day on a
    clock for the conflict advisory; the rest is what the default description
    (below) is built from.
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
    timezone = raw.get("timezone")
    return Attendee(
        name=(
            f"{first} {last}".strip()
            or str(raw.get("username") or "").strip()
            or "Unknown"
        ),
        timezone=timezone if isinstance(timezone, str) and timezone.strip() else None,
        title=_optional(raw.get("title")),
        city=_optional(raw.get("city")),
        state=_optional(raw.get("state")),
        username=str(raw.get("username") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Default description
# ---------------------------------------------------------------------------
# Ports the invite body the GoProfiles web scheduler itself sends when nobody
# customizes it (constants.js: generateUserDescription, plus the Google/Outlook
# templates that wrap it). The organizer's own person-block is dropped: this
# server can resolve the attendee's profile from a uid, but has no way to look
# up the organizer's own profile from a bearer token, so the intro is passive
# rather than naming them.


def _duration_phrase(minutes: int) -> str:
    """Adjectival duration for the intro line, e.g. '30 minute', '1 hour'.

    The source only maps 15/30/60 ('15 minute', '30 minute', '1 hour') and
    throws on anything else; this generalizes to whatever duration_minutes
    allows.
    """
    hours, mins = divmod(minutes, 60)
    if hours == 0:
        return f"{mins} minute"
    if mins == 0:
        return f"{hours} hour"
    return f"{hours} hour {mins} minute"


def _location(city: str | None, state: str | None) -> str | None:
    """City, state — either alone if only one is set. Country is not used,
    matching the source template."""
    parts = [p for p in (city, state) if p]
    return ", ".join(parts) if parts else None


def _profile_url(username: str) -> str:
    return f"https://www.goprofiles.io/profile?username={username}"


def _person_block(attendee: Attendee, *, line_break: str) -> str:
    """The 👤/📍/🔗 block the web scheduler puts in every invite.

    `line_break` is a real newline for Google's plain-text body, or the string
    "<br>" for Outlook's HTML one — every word is identical, only how the lines
    join differs.
    """
    header = f"👤 {attendee.name}"
    if attendee.title:
        header += f", {attendee.title}"
    lines = [header]

    location = _location(attendee.city, attendee.state)
    if location:
        lines.append(f"📍 Located in {location}")

    if attendee.username:
        url = _profile_url(attendee.username)
        lines.append(f'🔗 View profile: <a href="{url}">{url}</a>')

    return line_break.join(lines)


def _default_description(
    attendee: Attendee, duration_minutes: int, provider: str
) -> str:
    """The description preview_meeting stages when the caller doesn't supply one."""
    intro = f"You've been invited to a {_duration_phrase(duration_minutes)} meeting"
    footer = (
        'Booked on <a href="https://www.goprofiles.io" target="_blank">'
        "www.GoProfiles.io</a>"
    )

    if provider == "Outlook Calendar":
        block = _person_block(attendee, line_break="<br>")
        return f"<p>{intro}</p><p></p><p>{block}</p><p></p><p>{footer}</p>"

    block = _person_block(attendee, line_break="\n")
    return f"{intro}\n\n{block}\n\n{footer}"


# ---------------------------------------------------------------------------
# Conflict advisory
# ---------------------------------------------------------------------------


def _end_of_their_day(timezone_name: str | None) -> int | None:
    """Epoch at which the attendee's current local day ends, or None if unknown.

    The events feed is the remainder of *their* day, so a slot past this point is
    outside what the calendar read can see at all. A profile with no timezone
    makes the horizon unknowable, and an unknowable horizon means no slot can be
    called checked.
    """
    if not timezone_name:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        # A stale or misspelled IANA name should degrade to "can't check", not
        # fail the preview.
        return None

    tomorrow = datetime.now(zone) + timedelta(days=1)
    midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _overlaps(event: CalendarEvent, slot_start: int, slot_end: int) -> bool:
    # Strict on both ends, so a meeting that finishes exactly when this one
    # starts is not reported as a clash.
    return event.start_time < slot_end and event.end_time > slot_start


def _span(event: CalendarEvent, zone: tzinfo | None) -> str:
    display = zone or _DEFAULT_TZ
    start = datetime.fromtimestamp(event.start_time, tz=display)
    end = datetime.fromtimestamp(event.end_time, tz=display)
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _check_conflicts(
    probe: ProviderProbe,
    attendee: Attendee,
    slot_start: int,
    minutes: int,
    zone: tzinfo | None,
) -> ConflictCheck:
    """Advisory only — decide whether the attendee looks busy for this slot.

    Never blocks. The calendar API withholds event titles (everything is
    "busy"), so importance cannot be judged here, and double-booking is often
    deliberate. The user decides at the confirmation prompt; this only makes sure
    they decide knowing what the calendar showed.
    """
    slot_end = slot_start + minutes * 60

    if probe.outcome != "ok":
        return ConflictCheck(
            detail="their calendar could not be read, so conflicts are unknown"
        )
    if not probe.linked:
        return ConflictCheck(
            detail=(
                f"they have not connected their {probe.label}, so their meetings "
                "and time off are not visible"
            )
        )

    # Time off first: it is the stronger objection, and the only check here that
    # reaches beyond today — the provider looks two months ahead for it.
    if probe.ooo is not None and _overlaps(probe.ooo, slot_start, slot_end):
        display = zone or _DEFAULT_TZ
        first = datetime.fromtimestamp(probe.ooo.start_time, tz=display).strftime(
            "%d %b"
        )
        last = datetime.fromtimestamp(probe.ooo.end_time, tz=display).strftime("%d %b")
        window = first if first == last else f"{first} – {last}"
        return ConflictCheck(
            state="conflict",
            detail=f"they are scheduled OUT OF OFFICE then ({window})",
        )

    horizon = _end_of_their_day(attendee.timezone)

    # A clash is positive evidence and settles the question, so it is looked for
    # before asking how much of the slot was visible. Only meaningful while the
    # slot starts inside the day the feed covers — an all-day event today says
    # nothing about next Tuesday.
    if horizon is not None and slot_start < horizon:
        clashes = [
            e
            for e in probe.events
            # An all-day event blankets the day whatever its timestamps say;
            # get_availability documents that Outlook's are not to be trusted.
            if e.is_all_day or _overlaps(e, slot_start, slot_end)
        ]
        if clashes:
            busy = ", ".join(
                "all day" if e.is_all_day else _span(e, zone)
                for e in sorted(clashes, key=lambda e: (e.start_time, e.end_time))
            )
            return ConflictCheck(
                state="conflict",
                detail=f"they are already booked during this slot ({busy})",
            )

    if horizon is None:
        return ConflictCheck(
            detail=(
                "their profile has no timezone, so their meetings could not be "
                "lined up against this slot"
            )
        )
    # The feed stops at their midnight, so a slot reaching past it is at least
    # partly unseen. Every future booking lands here, and it must never read as
    # an all-clear.
    if slot_end > horizon:
        return ConflictCheck(
            detail=(
                (
                    "this slot is past the end of their current day"
                    if slot_start >= horizon
                    else "this slot runs past the end of their current day, so "
                    "the part after midnight was not checked"
                )
                + ", and only the rest of today's meetings are visible"
            )
        )

    return ConflictCheck(
        state="clear", detail="nothing on their calendar clashes with this slot"
    )


def _conflict_lines(check: ConflictCheck) -> str:
    """The Conflicts block for the preview, phrased so silence is never clearance."""
    if check.state == "conflict":
        return (
            f"Conflicts:   WARNING — {check.detail}.\n"
            "             This is advisory, not a block. Tell the user about the "
            "clash and let them decide whether to book it anyway.\n"
        )
    if check.state == "clear":
        return f"Conflicts:   None found — {check.detail}.\n"
    return (
        f"Conflicts:   NOT CHECKED — {check.detail}.\n"
        "             Do not tell the user this person is free. Say their "
        "availability could not be confirmed, or ask them to check.\n"
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
        str | None,
        Field(
            description=(
                "The invite body, shown to both people in the calendar event (max "
                "8192 characters; simple HTML is allowed). OMIT THIS to use the "
                "same default GoProfiles itself sends when nobody customizes it — "
                "a short note plus the attendee's name, title, location, and "
                "profile link. That default is the normal case; only pass a value "
                "here when the user has actually asked for different wording, or "
                "you've drafted something and shown it to them for approval. Do "
                "not invent custom text on their behalf."
            ),
            max_length=8192,
        ),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Preview a calendar invite before creating it. Creates nothing.

    Resolves the attendee to their real name, checks they exist, works out which
    calendar the workspace uses (Google or Outlook), and returns a preview of
    exactly what schedule_meeting will create. No event is created, no calendar
    is touched, and nobody is notified.

    The start time has to come from the user. Nothing here can search for a free
    slot on a future date — get_availability only reports what is left of that
    person's TODAY. So never pick a time on the user's behalf: ask them for the
    day and time, and put their UTC offset in starts_at.

    Omitting description uses the same default invite body the GoProfiles web
    scheduler sends when nobody customizes it — the attendee's name, title,
    location, and profile link, with a GoProfiles footer. That default is the
    normal case; don't draft custom text unless the user has actually asked for
    different wording.

    The result includes a Conflicts line, which is ADVISORY and never blocks the
    invite. It reports one of three things, and the difference matters: a warning
    that the attendee is booked or out of office; that nothing was found in the
    window that could be checked; or that the slot could not be checked at all —
    which is the normal answer for any slot beyond today, because only the rest
    of today's meetings are visible. Time off is the exception and is checked up
    to two months ahead. Relay that line as it stands. NOT CHECKED does not mean
    free, and must never be reported to the user as free.

    Ask for everything still missing in ONE message — the time, the length, and
    the title. Do not ask for these one turn at a time. Description needs no
    asking unless the user wants something other than the default.

    Then show the user the With / When / Duration / Title / Description from the
    result and wait for them to explicitly approve it. Only after that, call
    schedule_meeting.

    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    title = title.strip()
    if not title:
        return (
            "No preview — the title is empty. Ask the user what the meeting "
            "should be called, or offer to draft a title for their review."
        )

    epoch, zone, time_error = _resolve_start_at(starts_at)
    if time_error is not None:
        return f"No preview — {time_error}"

    attendee = await _attendee(uid, authorization, tool="preview_meeting")
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

    # Omitted or blank means "use the same default GoProfiles itself sends" —
    # built from the attendee's real profile, so it costs nothing beyond what
    # was already fetched above.
    description = (description or "").strip() or _default_description(
        attendee, duration_minutes, provider.label
    )

    # Advisory only, and computed from the payload the provider probe already
    # returned — so it costs nothing and never blocks the preview.
    conflicts = _check_conflicts(provider, attendee, epoch, duration_minutes, zone)

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
            "attendee_name": attendee.name,
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
        f"With:        {attendee.name}\n"
        f"When:        {_when_line(epoch, duration_minutes, zone)}\n"
        f"Duration:    {_duration_line(duration_minutes)}\n"
        f"Calendar:    {provider.label}\n"
        f"Title:       {title}\n" + _conflict_lines(conflicts) + "Description:\n"
        f"{description}\n\n"
        "Check the When line against what the user actually asked for. If the "
        "relative time ('about N hours from now') looks wrong, the UTC offset was "
        "wrong — ask them for their timezone and preview again.\n\n"
        "NEXT STEP — show the user the With / When / Duration / Title / "
        "Description above, relay the Conflicts line as it stands, and ask them to "
        "confirm. Do not call schedule_meeting until they explicitly say to send "
        "the invite. When they do, call schedule_meeting with attendee_name, "
        "starts_at, duration_minutes, title, and description copied exactly from "
        "this preview — starts_at must be the same value you passed here, or the "
        "invite will be refused. schedule_meeting takes no uid and no organizer: "
        "it creates the meeting previewed here, organized by the signed-in user."
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

    if await _attendee(sending["uid_to_meet"], authorization, tool=_TOOL) is None:
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
