from typing import Annotated, Literal

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
# Filter literals
# ---------------------------------------------------------------------------

CelebrationType = Literal["birthday", "anniversary", "new_hire"]

# The API caps every day window at MAX_ANALYTICS_DAYS; reject past it client-side
# so the model gets a schema error instead of a 422.
_MAX_DAYS = 365

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
# The API row also carries email and three image URLs. Leaving them off the model
# is the allow-list: pydantic drops unmodeled keys, so they never reach the agent.


class CelebrationResult(BaseModel):
    celebration: str = ""
    window: str = ""
    celebration_date: str | None = None
    days_ago: int | None = None
    days_until: int | None = None
    uid: int = 0
    first_name: str = ""
    last_name: str = ""
    username: str | None = None
    title: str | None = None
    department: str | None = None
    hired_at: str | None = None
    # Birthdays come back as 'MM-DD' — no year is stored.
    birthday: str | None = None
    years: int | None = None


class CelebrationsMetadata(BaseModel):
    limit: int = 0
    offset: int = 0
    total_results: int = 0
    count: int = 0


class ResolvedFilters(BaseModel):
    """The windows the API actually applied, echoed back on every response.

    Worth rendering: the defaults differ per celebration type, so a caller that
    passed no window can only learn which one produced these results from here.
    """

    celebration_types: list[str] = []
    past_days: int = 0
    upcoming_days: int = 0
    new_hire_days: int = 0


class CelebrationsResponse(BaseModel):
    metadata: CelebrationsMetadata = CelebrationsMetadata()
    filters: ResolvedFilters = ResolvedFilters()
    results: list[CelebrationResult] = []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _full_name(c: CelebrationResult) -> str:
    return f"{c.first_name} {c.last_name}".strip() or c.username or "Unknown"


def _celebration_label(c: CelebrationResult) -> str:
    if c.celebration == "birthday":
        return "Birthday"
    if c.celebration == "anniversary":
        return f"{c.years}-year work anniversary" if c.years else "Work anniversary"
    if c.celebration == "new_hire":
        return "New hire"
    return c.celebration or "Unknown"


def _when(c: CelebrationResult) -> str:
    """Relative phrasing for this occurrence. Past rows carry days_ago, upcoming
    rows days_until; either can be 0, meaning today."""
    if c.days_until is not None:
        if c.days_until == 0:
            return "today"
        return f"in {c.days_until} day{'s' if c.days_until != 1 else ''}"
    if c.days_ago is not None:
        if c.days_ago == 0:
            return "today"
        return f"{c.days_ago} day{'s' if c.days_ago != 1 else ''} ago"
    return "date unknown"


def _format_celebration(c: CelebrationResult) -> str:
    lines = [
        f"Name:        {_full_name(c)}",
        f"Username:    {c.username or 'Unknown'}",
        f"Celebration: {_celebration_label(c)}",
        f"Date:        {c.celebration_date or 'Unknown'} ({_when(c)})",
    ]
    # Stub accounts leave these blank; skip the line rather than printing
    # 'Unknown' on every row of a long feed.
    if c.title:
        lines.append(f"Title:       {c.title}")
    if c.department:
        lines.append(f"Department:  {c.department}")
    # Hire date is the substance of a new-hire or anniversary row, and noise on
    # a birthday row.
    if c.hired_at and c.celebration in ("new_hire", "anniversary"):
        lines.append(f"Started:     {c.hired_at}")
    # uid is for get_profile only — never repeat it in user-facing replies.
    lines.append(f"uid:         {c.uid}  (tool use only — do not show to the user)")
    return "\n".join(lines)


def _windows_summary(f: ResolvedFilters) -> str:
    """Describe the applied windows, including the new-hire look-back the API
    derives separately."""
    summary = f"{f.past_days} days back, {f.upcoming_days} days ahead"
    if "new_hire" in f.celebration_types and f.new_hire_days != f.past_days:
        summary += f"; new hires within {f.new_hire_days} days"
    return summary


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search_celebrations(
    celebration_types: Annotated[
        list[CelebrationType] | None,
        Field(
            description=(
                "Celebration types to include: 'birthday', 'anniversary' (work "
                "anniversary), and/or 'new_hire'. Omit to get all three in one "
                "result set — there is no separate new-hires tool."
            )
        ),
    ] = None,
    past_days: Annotated[
        int | None,
        Field(
            description=(
                "How many days back to look, 0–365. Defaults to 21 days for "
                "birthdays and anniversaries and 35 days for new hires. Setting it "
                "overrides BOTH, so past_days=7 also narrows new hires to 7 days, "
                "and past_days=0 returns upcoming celebrations only (excluding new "
                "hires entirely, since they have no upcoming window)."
            ),
            ge=0,
            le=_MAX_DAYS,
        ),
    ] = None,
    upcoming_days: Annotated[
        int | None,
        Field(
            description=(
                "How many days ahead to look, 0–365 (default 21). Applies to "
                "birthdays and anniversaries only — new hires are always in the past."
            ),
            ge=0,
            le=_MAX_DAYS,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Number of celebrations to return (1–100).", ge=1, le=100),
    ] = 50,
    offset: Annotated[int, Field(description="Pagination offset (0-based).", ge=0)] = 0,
    ctx: Context | None = None,
) -> str:
    """List upcoming and recent celebrations from the user's GoProfiles workspace
    (https://www.goprofiles.io): birthdays, work anniversaries, and new hires.

    All three types come back from this one call as a single merged feed — recent
    celebrations first (most recent first), then upcoming ones (soonest first).
    Filter with 'celebration_types' rather than making separate calls, and note
    there is no separate new-hires tool.

    Date filtering is by relative day windows, not calendar dates: 'past_days'
    looks back and 'upcoming_days' looks ahead from today. Each result includes
    the absolute 'celebration_date', so pick a window wide enough to cover the
    dates the user asked about and read the exact dates off the results.

    Each entry carries a numeric 'uid' for follow-up get_profile calls. Never
    show, read aloud, or otherwise expose 'uid' values to the user; refer to
    people by name, username, title, or department instead.

    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    raw_params: dict = {"celebrations": "true", "limit": limit, "offset": offset}

    # Comma-separated string, not repeated params — that's what the API parses.
    if celebration_types:
        raw_params["celebration_types"] = ",".join(celebration_types)

    # `is not None`, not truthiness: 0 is a meaningful window the API honors.
    if past_days is not None:
        raw_params["past_days"] = past_days
    if upcoming_days is not None:
        raw_params["upcoming_days"] = upcoming_days

    params = external_params(raw_params, tool="search_celebrations")

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

    raise_for_status(response, "/users.php")

    data = CelebrationsResponse.model_validate(response.json())
    f = data.filters
    types = ", ".join(f.celebration_types) or "birthday, anniversary, new_hire"

    # An empty feed is a normal outcome — say which filters produced it, since the
    # windows may be API defaults the caller never passed.
    if not data.results:
        return (
            f"No celebrations found for {types} within the searched window "
            f"({_windows_summary(f)}). Widen the range with past_days/upcoming_days, "
            "or bear in mind that birthdays and hire dates are blank on some profiles."
        )

    m = data.metadata
    header = (
        f"Celebrations ({m.count} of {m.total_results} total, offset {m.offset}) — "
        f"types: {types}; {_windows_summary(f)}:"
    )

    sections: list[str] = []
    index = 1
    for window, label in (("past", "Recent"), ("upcoming", "Upcoming")):
        rows = [c for c in data.results if c.window == window]
        if not rows:
            continue
        entries = []
        for c in rows:
            entries.append(f"[{index}]\n{_format_celebration(c)}")
            index += 1
        sections.append(f"{label}:\n\n" + "\n\n".join(entries))

    # Any row with an unexpected 'window' would otherwise vanish from the output.
    leftover = [c for c in data.results if c.window not in ("past", "upcoming")]
    for c in leftover:
        sections.append(f"[{index}]\n{_format_celebration(c)}")
        index += 1

    return header + "\n\n" + "\n\n".join(sections)
