import json
from typing import Annotated, Literal

import httpx
from fastmcp import Context
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    SortOrder,
    external_params,
    get_authorization_header,
    http_client,
    raise_for_status,
)

# ---------------------------------------------------------------------------
# Filter/sort literals
# ---------------------------------------------------------------------------

PeopleSort = Literal["best_match", "hired_at"]

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PersonResult(BaseModel):
    uid: int = 0
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    title: str | None = None
    department_name: str | None = None
    location: str | None = None
    domain: str | None = None
    reports: int = 0
    unlicensed_profile: int | None = None
    # GROUP_CONCAT'd on the API side — comma-separated, not JSON arrays.
    user_skills: str | None = None
    user_interests: str | None = None
    user_groups: str | None = None
    user_languages: str | None = None
    # Relevance score. LOWER is better; see _match_quality.
    rating: float = 0.0


class PeoplePaginationMetadata(BaseModel):
    limit: int = 0
    offset: int = 0
    total_results: int = 0
    count: int = 0


class PeopleSearchResponse(BaseModel):
    metadata: PeoplePaginationMetadata = PeoplePaginationMetadata()
    results: list[PersonResult] = []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# Rating bands assigned by the search API (lower is better). Name hits occupy the
# 1.x band so they always outrank an attribute hit; everything >= 2 matched on
# something other than the person's name.
_RATING_EXACT_NAME = 1.16  # exact first / "first last" / last / username
_RATING_PARTIAL_NAME = 2.0  # prefix / suffix / substring of a name


def _match_quality(rating: float) -> str:
    """Describe why a person is in the result set, so the agent can tell an
    exact name hit apart from an incidental attribute hit."""
    if rating < _RATING_EXACT_NAME:
        return "exact name match"
    if rating < _RATING_PARTIAL_NAME:
        return "partial name match"
    return "matched on title/department/skill/etc., not name"


def _full_name(p: PersonResult) -> str:
    return f"{p.first_name} {p.last_name}".strip() or p.username or "Unknown"


def _format_person(p: PersonResult, *, show: set[str]) -> str:
    lines = [
        f"UID:        {p.uid}",
        f"Name:       {_full_name(p)}",
        f"Username:   {p.username or 'Unknown'}",
        f"Title:      {p.title or 'Unknown'}",
        f"Department: {p.department_name or 'Unknown'}",
        f"Location:   {p.location or 'Unknown'}",
    ]

    # Only echo the facets the caller filtered on — otherwise every result drags
    # along four long comma-separated lists and buries the identifying fields.
    facets = [
        ("skills", "Skills", p.user_skills),
        ("interests", "Interests", p.user_interests),
        ("languages", "Languages", p.user_languages),
        ("groups", "Groups", p.user_groups),
    ]
    for key, label, value in facets:
        if key in show and value:
            lines.append(f"{label + ':':<12}{value}")

    if p.reports:
        lines.append(f"Reports:    {p.reports} direct report(s)")
    if p.unlicensed_profile:
        lines.append("Note:       Unlicensed profile — may have incomplete data.")

    lines.append(f"Match:      {_match_quality(p.rating)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search_people(
    names: Annotated[
        list[str] | None,
        Field(
            description=(
                "Name fragments to match against a person's full name ('first last') "
                "or username. Matching is case-insensitive SUBSTRING matching, NOT fuzzy "
                "— a misspelling will not match. Pass several variants and let the "
                "server pick the best: for 'Zack Dahl' send "
                "['zack dahl', 'zach', 'zac', 'dahl', 'doll']. When a first name may be "
                "shortened, send the short form ('zach' matches 'Zachary'); when unsure "
                "of a spelling, send the shortest confident fragment plus each plausible "
                "spelling. Omit entirely to list everyone matching the other filters."
            )
        ),
    ] = None,
    departments: Annotated[
        list[str] | None,
        Field(
            description=(
                "Exact department names to filter by, e.g. ['Engineering', 'Customer Success']. "
                "Matched exactly, so use the names shown in earlier results."
            )
        ),
    ] = None,
    titles: Annotated[
        list[str] | None,
        Field(
            description=(
                "Exact job titles to filter by, e.g. ['Software Engineer', 'Sales Director']. "
                "Matched exactly — to find people by an approximate role, put the word in "
                "'names' instead, which also searches titles."
            )
        ),
    ] = None,
    locations: Annotated[
        list[str] | None,
        Field(
            description=(
                "Locations to filter by, e.g. ['Texas, United States', 'North Holland, Netherlands']. "
                "Matched as a substring of the person's 'city, state, country', so 'United States' "
                "matches everyone in the US."
            )
        ),
    ] = None,
    skills: Annotated[
        list[str] | None,
        Field(description="Exact skill names to filter by, e.g. ['Python', 'Kubernetes']."),
    ] = None,
    interests: Annotated[
        list[str] | None,
        Field(description="Exact interest names to filter by, e.g. ['Hiking', 'Cooking']."),
    ] = None,
    languages: Annotated[
        list[str] | None,
        Field(
            description=(
                "Exact language names to filter by, e.g. ['Spanish', 'English (Australian)']. "
                "Use language names, not ISO codes."
            )
        ),
    ] = None,
    groups: Annotated[
        list[str] | None,
        Field(description="Exact group names to filter by, e.g. ['Onboarding Buddies']."),
    ] = None,
    limit: Annotated[
        int, Field(description="Number of people to return (1–100).", ge=1, le=100)
    ] = 20,
    offset: Annotated[int, Field(description="Pagination offset (0-based).", ge=0)] = 0,
    sort: Annotated[
        PeopleSort | None,
        Field(
            description=(
                "'best_match' (default) ranks exact name matches first; 'hired_at' sorts "
                "by hire date."
            )
        ),
    ] = None,
    order: Annotated[
        SortOrder | None,
        Field(description="Sort direction: 'asc' or 'desc'. Only applies when sort='hired_at'."),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Search and filter people in the user's GoProfiles directory
    (https://www.goprofiles.io) by name, department, title, location, language,
    skill, interest, and group. Returns each person's numeric 'uid' along with
    their title, department, and location.

    This is the name-to-uid resolution step: call it first whenever another tool
    needs a 'uid' and you only have a name. Pass the name to 'names', then pick
    the uid from the results — use title/department/location to disambiguate when
    several people match, and ask the user which one they meant rather than
    guessing between two plausible matches.

    Name matching is substring-based, not fuzzy, so pass several spelling and
    shortening variants in 'names' at once. Results are ranked best-first and each
    entry reports its match quality; a name query can also surface people whose
    title or skill contains the term, which is flagged as a non-name match.

    Read-only. Requires search:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    raw_params: dict = {"limit": limit, "offset": offset}

    # The API expects list filters as JSON-encoded strings, not repeated params.
    # search_terms=[] is the documented match-all form, so filter-only searches
    # (no names) still work.
    raw_params["search_terms"] = json.dumps(names or [])

    json_filters = [
        ("departments", departments),
        ("titles", titles),
        ("locations", locations),
        ("skills", skills),
        ("interests", interests),
        ("languages", languages),
        ("groups", groups),
    ]
    for key, value in json_filters:
        if value:
            raw_params[key] = json.dumps(value)

    if sort is not None:
        raw_params["sort"] = sort
    # The API only honors order for hired_at; sending it otherwise is a no-op.
    if order is not None:
        raw_params["order"] = order

    params = external_params(raw_params, tool="search_people")

    try:
        response = await http_client.get(
            "/search/users.php",
            params=params,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, "/search/users.php")

    data = PeopleSearchResponse.model_validate(response.json())

    if not data.results:
        if names:
            return (
                "No people found. Name matching is substring-based, so a misspelling "
                "returns nothing — retry with a shorter fragment (e.g. 'dah' instead of "
                "'dahlberg'), the last name alone, or additional spelling variants."
            )
        return "No people found."

    show = {key for key, value in json_filters if value} & {
        "skills",
        "interests",
        "languages",
        "groups",
    }

    m = data.metadata
    header = f"People ({m.count} of {m.total_results} total, offset {m.offset}):\n"
    entries = [
        f"[{i}]\n{_format_person(p, show=show)}"
        for i, p in enumerate(data.results, 1)
    ]
    footer = ""
    if names and len(data.results) > 1:
        footer = (
            "\n\nMultiple people matched. Use title/department/location to pick the "
            "right uid, and confirm with the user if the match is ambiguous."
        )
    return header + "\n\n".join(entries) + footer
