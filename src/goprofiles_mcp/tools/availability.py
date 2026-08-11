import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastmcp import Context
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    api_get,
    external_params,
    get_authorization_header,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The two calendar integrations GoProfiles supports. A workspace enables at most
# one; the other answers 403, which is how we tell which is in play.
_PROVIDERS = (
    ("/google-calendar", "Google Calendar"),
    ("/outlook-calendar", "Outlook Calendar"),
)

# Index order matches the working_hours_<day>_<start|end> column names on
# users.php, which follow MySQL's 0=Sunday convention.
_DAY_KEYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DAY_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

_MINUTES_PER_DAY = 1440

# An opening shorter than this is not worth surfacing as a meeting slot.
_MIN_BLOCK_SECONDS = 15 * 60

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CalendarEvent(BaseModel):
    title: str = "busy"
    start_time: int = 0
    end_time: int = 0
    # Google omits this field entirely; only Outlook sends it.
    is_all_day: bool = False


class CalendarResponse(BaseModel):
    # Google sets status to 'ok' or 'failure'. Outlook never sends it at all,
    # so an empty status on a 200 still means success.
    status: str = ""
    error: str = ""
    events: list[CalendarEvent] = []
    # PHP encodes "no OOO event" as an empty array, so this is a dict when set
    # and a list when not. Normalize through _as_event rather than typing it.
    ooo: Any = None


class WorkingHours(BaseModel):
    """A person's configured schedule, as minutes since midnight in their own tz."""

    # Day index (0=Sun) -> (start, end). A missing key is a non-working day.
    days: dict[int, tuple[int, int]] = {}
    timezone: str | None = None


class ProviderReading(BaseModel):
    """What one calendar endpoint told us about this person."""

    # ok | not_enabled | not_connected | error
    outcome: str = "error"
    provider: str = ""
    events: list[CalendarEvent] = []
    ooo: CalendarEvent | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _as_event(raw: Any) -> CalendarEvent | None:
    """Coerce an `ooo` payload to an event, treating PHP's empty array as absent."""
    if not isinstance(raw, dict) or not raw:
        return None
    event = CalendarEvent.model_validate(raw)
    if not event.start_time and not event.end_time:
        return None
    return event


def _parse_working_hours(user_raw: dict) -> WorkingHours:
    """Pull the 14 working_hours_* columns and timezone off a users.php payload."""
    days: dict[int, tuple[int, int]] = {}
    for index, key in enumerate(_DAY_KEYS):
        start = user_raw.get(f"working_hours_{key}_start")
        end = user_raw.get(f"working_hours_{key}_end")
        # Both halves are required — a half-populated row is not a usable window.
        if start is None or end is None:
            continue
        try:
            days[index] = (int(start), int(end))
        except (TypeError, ValueError):
            continue

    timezone = user_raw.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        timezone = None

    return WorkingHours(days=days, timezone=timezone)


def _resolve_zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A stale or misspelled IANA name should degrade to "unknown timezone",
        # not fail the whole availability lookup.
        return None


# ---------------------------------------------------------------------------
# Working-hours math
# ---------------------------------------------------------------------------


def _local_day_index(moment: datetime) -> int:
    """Day-of-week as 0=Sunday, matching the users.php column naming."""
    # Python's weekday() is 0=Monday; shift it into the Sunday-first convention.
    return (moment.weekday() + 1) % 7


def _minutes_to_unix(day_start: datetime, minutes: int) -> int:
    """Absolute timestamp for `minutes` past the given local midnight."""
    return int((day_start + timedelta(minutes=minutes)).timestamp())


def _todays_window(now_local: datetime, hours: WorkingHours) -> tuple[int, int] | None:
    """Today's working window as unix bounds, or None on a non-working day.

    Two shapes produce a window that covers `now`: today's own entry, and
    yesterday's when it was an overnight shift whose tail runs past midnight
    into this morning. This mirrors isInWorkHoursRange in the product UI.
    """
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = _local_day_index(now_local)
    current_minutes = now_local.hour * 60 + now_local.minute

    yesterday = (today + 6) % 7
    yesterday_entry = hours.days.get(yesterday)
    if (
        yesterday_entry
        and yesterday_entry[0] > yesterday_entry[1]
        and current_minutes < yesterday_entry[1]
    ):
        # We are inside the post-midnight half of yesterday's shift. The window
        # that matters started yesterday, so anchor it to yesterday's midnight.
        prior_midnight = midnight - timedelta(days=1)
        return (
            _minutes_to_unix(prior_midnight, yesterday_entry[0]),
            _minutes_to_unix(prior_midnight, yesterday_entry[1] + _MINUTES_PER_DAY),
        )

    entry = hours.days.get(today)
    if entry is None:
        return None

    start, end = entry
    # An end before the start means the shift wraps into tomorrow morning.
    if start > end:
        end += _MINUTES_PER_DAY
    return _minutes_to_unix(midnight, start), _minutes_to_unix(midnight, end)


def _merge_busy(
    events: list[CalendarEvent], window: tuple[int, int]
) -> list[tuple[int, int]]:
    """Clip events to the window, then merge overlapping and touching intervals.

    Events arrive unsorted and may overlap each other. An Outlook all-day event
    carries real timestamps but should blanket the window regardless of them.
    """
    window_start, window_end = window
    intervals: list[tuple[int, int]] = []
    for event in events:
        if event.is_all_day:
            intervals.append(window)
            continue
        start = max(event.start_time, window_start)
        end = min(event.end_time, window_end)
        if end > start:
            intervals.append((start, end))

    intervals.sort()
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _open_blocks(
    now_unix: int, window: tuple[int, int], busy: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Subtract merged busy intervals from what remains of the working window."""
    cursor = max(window[0], now_unix)
    window_end = window[1]
    if cursor >= window_end:
        return []

    blocks: list[tuple[int, int]] = []
    for start, end in busy:
        if end <= cursor:
            continue
        if start > cursor:
            blocks.append((cursor, min(start, window_end)))
        cursor = max(cursor, end)
        if cursor >= window_end:
            break

    if cursor < window_end:
        blocks.append((cursor, window_end))

    return [(s, e) for s, e in blocks if e - s >= _MIN_BLOCK_SECONDS]


def _current_meeting(
    now_unix: int, events: list[CalendarEvent]
) -> CalendarEvent | None:
    for event in events:
        if event.start_time <= now_unix <= event.end_time:
            return event
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_clock(ts: int, zone: ZoneInfo) -> str:
    # %-I is a GNU/BSD extension; both the slim image and macOS support it.
    return datetime.fromtimestamp(ts, tz=zone).strftime("%-I:%M %p")


def _format_minutes(minutes: int) -> str:
    hour, minute = divmod(minutes % _MINUTES_PER_DAY, 60)
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix}"


def _format_schedule(hours: WorkingHours) -> str:
    """Render the weekly schedule, collapsing days that share the same window."""
    if not hours.days:
        return "Unknown"

    groups: list[tuple[tuple[int, int], list[str]]] = []
    for index in range(7):
        entry = hours.days.get(index)
        if entry is None:
            continue
        if groups and groups[-1][0] == entry:
            groups[-1][1].append(_DAY_LABELS[index])
        else:
            groups.append((entry, [_DAY_LABELS[index]]))

    parts = []
    for (start, end), labels in groups:
        span = labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"
        parts.append(f"{_format_minutes(start)} – {_format_minutes(end)} ({span})")
    return "; ".join(parts)


def _format_meetings(
    events: list[CalendarEvent],
    now_unix: int,
    display_zone: ZoneInfo,
    zone_suffix: str,
) -> list[str]:
    """One line per remaining meeting, in order.

    Deliberately NOT merged: merging is only for computing free gaps, and a
    merged block hides how a long busy stretch is actually divided up. Two
    entries can overlap — that means the person is double-booked.
    """
    upcoming = [event for event in events if event.end_time > now_unix]
    if not upcoming:
        return ["None"]

    rendered: list[str] = []
    for event in sorted(upcoming, key=lambda e: (e.start_time, e.end_time)):
        if event.is_all_day:
            rendered.append("All day")
            continue
        start = _format_clock(event.start_time, display_zone)
        end = _format_clock(event.end_time, display_zone)
        line = f"{start} – {end}{zone_suffix}"
        # Flag the one covering now so "busy until X" is traceable to a meeting.
        if event.start_time <= now_unix <= event.end_time:
            line += "  (in progress)"
        rendered.append(line)
    return rendered


def _format_ooo(ooo: CalendarEvent | None, zone: ZoneInfo | None) -> str:
    if ooo is None:
        return "None scheduled"
    tz = zone or UTC
    start = datetime.fromtimestamp(ooo.start_time, tz=tz).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(ooo.end_time, tz=tz).strftime("%Y-%m-%d")
    return start if start == end else f"{start} to {end}"


# ---------------------------------------------------------------------------
# Calendar fetching
# ---------------------------------------------------------------------------


async def _read_provider(
    path: str, provider: str, uid: int, authorization: str
) -> ProviderReading:
    """Fetch one provider, mapping every failure mode onto an outcome.

    Nothing here raises: a workspace runs one provider, so the other one's 403 is
    the expected case, not an error worth aborting the tool for.
    """
    params = external_params(
        {"uid": uid, "events": 1, "ooo": 1}, tool="get_availability"
    )
    try:
        response = await api_get(path, params, authorization)
    except PermissionError:
        # 403 — this integration is not turned on for the workspace. (A bad token
        # would have failed the users.php call we already made.)
        return ProviderReading(outcome="not_enabled", provider=provider)
    except LookupError:
        # 404 — Outlook's answer for a person with no mailbox in the tenant. The
        # person themselves is already known to exist from users.php.
        return ProviderReading(outcome="not_connected", provider=provider)
    except (TimeoutError, ConnectionError, RuntimeError, ValueError) as exc:
        return ProviderReading(outcome="error", provider=provider, detail=str(exc))

    data = CalendarResponse.model_validate(response.json())

    # Google reports a disconnected person in the body of a 200 rather than a
    # status code. Outlook never sends `status`, so only 'failure' is a signal.
    if data.status == "failure":
        return ProviderReading(
            outcome="not_connected", provider=provider, detail=data.error
        )

    return ProviderReading(
        outcome="ok",
        provider=provider,
        events=data.events,
        ooo=_as_event(data.ooo),
    )


def _pick_reading(readings: list[ProviderReading]) -> ProviderReading:
    """Choose the reading that describes this workspace.

    Preference order is strongest signal first: a provider that answered, then
    one that is enabled but unlinked, then an outright error. Everything
    disabled means the workspace has no calendar integration at all.
    """
    for outcome in ("ok", "not_connected", "error"):
        for reading in readings:
            if reading.outcome == outcome:
                return reading
    return ProviderReading(outcome="not_enabled")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def get_availability(
    uid: Annotated[
        int | None,
        Field(
            description=(
                "Numeric user id of the person, from a search_people call in THIS "
                "chat. There is no default and no 'current user' — without a uid "
                "this tool returns nothing, so run search_people first instead of "
                "calling with no arguments. Pass the uid only — never show it to "
                "the user."
            ),
            ge=1,
        ),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Check whether a specific person is available right now, by uid
    (https://www.goprofiles.io).

    Combines their connected calendar (Google or Outlook, whichever the workspace
    uses) with the working hours configured on their GoProfiles profile: their
    local time, whether they are on the clock, the start and end time of every
    meeting left in their day, the open blocks between those meetings, and their
    next time off.

    Meeting times only — the calendar API deliberately withholds titles,
    locations, and attendees, so never claim to know what a meeting is about.

    Scoped to today — it reports what is left of the person's current working
    day, not future dates. Use after search_people has resolved a name to a uid.
    If the person has no calendar connected, or the workspace has no calendar
    integration, this still reports their working hours and says so explicitly;
    relay that caveat rather than presenting the hours as confirmed free time.

    Never show the uid to the user. Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    # Same reasoning as get_profile: a schema error for a missing uid tells the
    # model nothing it can act on, so answer with the next step instead.
    if uid is None:
        return (
            "No availability fetched — get_availability requires a uid and has no "
            "default. Call search_people with the person's name to get their uid, "
            "then call get_availability again with it. If the user asked about "
            "their own availability, ask them for their name first: this server "
            "cannot identify the signed-in user."
        )

    not_found = (
        "No profile found for that person, so their availability can't be "
        "checked. Confirm the match from search_people and try again."
    )

    # Working hours and timezone come from the profile, not the calendar — they
    # are the half of the picture that exists even with no calendar connected.
    user_params = external_params({"uid": uid}, tool="get_availability")
    try:
        user_response = await api_get(
            "/users.php", user_params, authorization, not_found_message=not_found
        )
    except LookupError:
        return not_found

    user_raw = user_response.json()
    if not isinstance(user_raw, dict) or not user_raw.get("uid"):
        return not_found

    hours = _parse_working_hours(user_raw)
    zone = _resolve_zone(hours.timezone)

    # Both providers are probed together: only one is enabled per workspace, and
    # which one is not knowable up front without a second round trip.
    readings = await asyncio.gather(
        *(
            _read_provider(path, provider, uid, authorization)
            for path, provider in _PROVIDERS
        )
    )
    reading = _pick_reading(list(readings))

    header = (
        f"Availability ({reading.provider})"
        if reading.outcome == "ok"
        else "Availability (calendar unavailable)"
    )

    now = datetime.now(tz=UTC)
    now_unix = int(now.timestamp())
    lines = [f"{header}:"]

    if zone is None:
        lines.append("Local time:     Unknown (no timezone set on their profile)")
    else:
        lines.append(
            f"Local time:     {_format_clock(now_unix, zone)} ({hours.timezone})"
        )

    lines.append(f"Working hours:  {_format_schedule(hours)}")

    meeting = _current_meeting(now_unix, reading.events)

    # Clock times fall back to UTC when the profile carries no timezone. Say so
    # on every rendered time — an unlabeled hour reads as the person's local one.
    display_zone = zone or UTC
    zone_suffix = "" if zone else " UTC"

    # The gap math needs a timezone to place the working window on the clock.
    # Without one, report what the calendar alone can support.
    window = _todays_window(now.astimezone(zone), hours) if zone else None

    if reading.outcome != "ok":
        status = "Unknown — no calendar data"
    elif meeting is not None:
        end = _format_clock(meeting.end_time, display_zone)
        status = f"In a meeting until {end}{zone_suffix}"
    elif window is None:
        status = "Outside working hours"
    elif window[0] <= now_unix < window[1]:
        status = "Free now, within working hours"
    else:
        status = "Outside working hours"
    lines.append(f"Status:         {status}")

    # The meetings themselves, before any merging — this is what tells the user
    # how a long busy stretch breaks down, which the free gaps alone can't show.
    if reading.outcome == "ok":
        meetings = _format_meetings(reading.events, now_unix, display_zone, zone_suffix)
        lines.append(f"Meetings left:  {meetings[0]}")
        # Continuations line up under the first entry's column.
        lines.extend(f"{'':16}{line}" for line in meetings[1:])

    if window is None:
        # Distinguish "we can't place the day on a clock" from "today is a day off".
        open_text = (
            "Unknown — no timezone set, so their day can't be placed on a clock"
            if zone is None
            else "None — not a working day for them"
        )
    else:
        busy = _merge_busy(reading.events, window) if reading.outcome == "ok" else []
        blocks = _open_blocks(now_unix, window, busy)
        if not blocks:
            open_text = "None left today"
        else:
            open_text = ", ".join(
                f"{_format_clock(s, display_zone)} – {_format_clock(e, display_zone)}"
                for s, e in blocks
            )
        # Without calendar data these blocks are working hours, not free time.
        if reading.outcome != "ok" and blocks:
            open_text += " (working hours only — meetings not visible)"
    lines.append(f"Open today:     {open_text}")

    if reading.outcome == "ok":
        lines.append(f"Next time off:  {_format_ooo(reading.ooo, zone)}")

    if reading.outcome == "not_enabled":
        lines.append(
            "Note:           No calendar integration is connected to this "
            "GoProfiles workspace, so meetings can't be checked. The hours above "
            "are this person's configured schedule only."
        )
    elif reading.outcome == "not_connected":
        lines.append(
            f"Note:           This person has not connected their "
            f"{reading.provider}, so their meetings aren't visible. The hours "
            "above are their configured schedule only."
        )
    elif reading.outcome == "error":
        lines.append(
            f"Note:           {reading.provider} could not be reached, so "
            "meetings aren't visible. The hours above are this person's "
            "configured schedule only."
        )

    return "\n".join(lines)
