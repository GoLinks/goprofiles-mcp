import asyncio
import json
from typing import Annotated, Any, Literal

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
# get_profile models + allow-lists
# ---------------------------------------------------------------------------
# Explicit allow-lists: only these keys are copied from the underlying PHP
# endpoints into the normalized profile. Everything else is dropped before
# validation so internal/sensitive fields never reach the agent.

_USER_SCALAR_FIELDS = frozenset(
    {
        "uid",
        "first_name",
        "last_name",
        "username",
        "title",
        "department",
        "intro",
        "pronouns",
        "city",
        "state",
        "country",
        "timezone",
        "email",
        "phone",
        "phone_extension",
        "personal_phone",
        "slack",
        "linkedin",
        "twitter",
        "github",
        "personal_website",
        "profile_link",
        "unlicensed_profile",
    }
)
_USER_LIST_FIELDS = frozenset({"skills", "interests", "groups"})
_NAMED_ITEM_FIELDS = frozenset({"name"})
_LANGUAGE_FIELDS = frozenset({"name", "code"})
_CERTIFICATION_FIELDS = frozenset(
    {
        "name",
        "category",
        "issue_date",
        "expiration_date",
        "credential_id",
    }
)


class ProfileContact(BaseModel):
    email: str | None = None
    phone: str | None = None
    phone_extension: str | None = None
    personal_phone: str | None = None
    slack: str | None = None
    linkedin: str | None = None
    twitter: str | None = None
    github: str | None = None
    personal_website: str | None = None


class ProfileCertification(BaseModel):
    name: str = ""
    category: str | None = None
    issue_date: str | None = None
    expiration_date: str | None = None
    credential_id: str | None = None


class ProfileLanguage(BaseModel):
    name: str = ""
    code: str = ""


class Profile(BaseModel):
    uid: int = 0
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    title: str | None = None
    department: str | None = None
    bio: str | None = None
    pronouns: str | None = None
    location: str | None = None
    timezone: str | None = None
    contact: ProfileContact = ProfileContact()
    skills: list[str] = []
    interests: list[str] = []
    groups: list[str] = []
    languages: list[ProfileLanguage] = []
    certifications: list[ProfileCertification] = []
    profile_link: str | None = None
    unlicensed_profile: int | None = None


class CertificationsResponse(BaseModel):
    results: list[dict[str, Any]] = []


class LanguagesResponse(BaseModel):
    results: list[dict[str, Any]] = []


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


def _format_person(p: PersonResult, *, show: set[str], show_match: bool) -> str:
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

    # Match quality only applies to name search. Filter-only calls may omit rating
    # (default 0.0), which would otherwise look like an exact name hit.
    if show_match:
        lines.append(f"Match:      {_match_quality(p.rating)}")
    return "\n".join(lines)


def _allow_fields(raw: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Copy only allow-listed keys from an API object."""
    return {key: raw[key] for key in allowed if key in raw}


def _names_from_items(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _allow_fields(item, _NAMED_ITEM_FIELDS).get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _format_location(city: Any, state: Any, country: Any) -> str | None:
    parts = [
        p.strip() for p in (city, state, country) if isinstance(p, str) and p.strip()
    ]
    return ", ".join(parts) if parts else None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_profile(
    user_raw: dict[str, Any],
    certifications_raw: list[dict[str, Any]],
    languages_raw: list[dict[str, Any]],
) -> Profile:
    """Combine users.php + certifications.php + languages.php into one schema."""
    user = _allow_fields(user_raw, _USER_SCALAR_FIELDS)
    lists = _allow_fields(user_raw, _USER_LIST_FIELDS)

    contact = ProfileContact.model_validate(
        {
            "email": _optional_str(user.get("email")),
            "phone": _optional_str(user.get("phone")),
            "phone_extension": _optional_str(user.get("phone_extension")),
            "personal_phone": _optional_str(user.get("personal_phone")),
            "slack": _optional_str(user.get("slack")),
            "linkedin": _optional_str(user.get("linkedin")),
            "twitter": _optional_str(user.get("twitter")),
            "github": _optional_str(user.get("github")),
            "personal_website": _optional_str(user.get("personal_website")),
        }
    )

    certifications = [
        ProfileCertification.model_validate(_allow_fields(item, _CERTIFICATION_FIELDS))
        for item in certifications_raw
        if isinstance(item, dict)
    ]
    languages = [
        ProfileLanguage.model_validate(_allow_fields(item, _LANGUAGE_FIELDS))
        for item in languages_raw
        if isinstance(item, dict)
    ]

    return Profile(
        uid=int(user.get("uid") or 0),
        first_name=str(user.get("first_name") or ""),
        last_name=str(user.get("last_name") or ""),
        username=str(user.get("username") or ""),
        title=_optional_str(user.get("title")),
        department=_optional_str(user.get("department")),
        bio=_optional_str(user.get("intro")),
        pronouns=_optional_str(user.get("pronouns")),
        location=_format_location(
            user.get("city"), user.get("state"), user.get("country")
        ),
        timezone=_optional_str(user.get("timezone")),
        contact=contact,
        skills=_names_from_items(lists.get("skills")),
        interests=_names_from_items(lists.get("interests")),
        groups=_names_from_items(lists.get("groups")),
        languages=languages,
        certifications=certifications,
        profile_link=_optional_str(user.get("profile_link")),
        unlicensed_profile=user.get("unlicensed_profile"),
    )


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "None"


def _format_profile(p: Profile) -> str:
    name = f"{p.first_name} {p.last_name}".strip() or p.username or "Unknown"
    c = p.contact
    languages = (
        ", ".join(
            f"{lang.name} ({lang.code})" if lang.code else lang.name
            for lang in p.languages
            if lang.name
        )
        or "None"
    )
    if p.certifications:
        cert_lines = []
        for cert in p.certifications:
            detail = cert.name or "Unknown"
            extras = [
                x
                for x in (
                    cert.category,
                    f"issued {cert.issue_date}" if cert.issue_date else None,
                    f"expires {cert.expiration_date}" if cert.expiration_date else None,
                    f"id {cert.credential_id}" if cert.credential_id else None,
                )
                if x
            ]
            if extras:
                detail = f"{detail} ({'; '.join(extras)})"
            cert_lines.append(f"  - {detail}")
        certifications = "\n" + "\n".join(cert_lines)
    else:
        certifications = "None"

    lines = [
        f"UID:           {p.uid}",
        f"Name:          {name}",
        f"Username:      {p.username or 'Unknown'}",
        f"Title:         {p.title or 'Unknown'}",
        f"Department:    {p.department or 'Unknown'}",
        f"Location:      {p.location or 'Unknown'}",
        f"Timezone:      {p.timezone or 'Unknown'}",
        f"Pronouns:      {p.pronouns or 'Unknown'}",
        f"Bio:           {p.bio or 'None'}",
        f"Profile link:  {p.profile_link or 'Unknown'}",
        "Contact:",
        f"  Email:             {c.email or 'Unknown'}",
        f"  Phone:             {c.phone or 'Unknown'}",
        f"  Phone extension:   {c.phone_extension or 'None'}",
        f"  Personal phone:    {c.personal_phone or 'Unknown'}",
        f"  Slack:             {c.slack or 'Unknown'}",
        f"  LinkedIn:          {c.linkedin or 'Unknown'}",
        f"  Twitter:           {c.twitter or 'Unknown'}",
        f"  GitHub:            {c.github or 'Unknown'}",
        f"  Personal website:  {c.personal_website or 'Unknown'}",
        f"Skills:        {_format_list(p.skills)}",
        f"Interests:     {_format_list(p.interests)}",
        f"Groups:        {_format_list(p.groups)}",
        f"Languages:     {languages}",
        f"Certifications: {certifications}",
    ]
    if p.unlicensed_profile:
        lines.append("Note:          Unlicensed profile — may have incomplete data.")
    return "\n".join(lines)


async def _api_get(
    path: str,
    params: dict,
    authorization: str,
    *,
    not_found_message: str | None = None,
) -> httpx.Response:
    try:
        response = await http_client.get(
            path,
            params=params,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, path, not_found_message=not_found_message)
    return response


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
        Field(
            description="Exact skill names to filter by, e.g. ['Python', 'Kubernetes']."
        ),
    ] = None,
    interests: Annotated[
        list[str] | None,
        Field(
            description="Exact interest names to filter by, e.g. ['Hiking', 'Cooking']."
        ),
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
        Field(
            description="Exact group names to filter by, e.g. ['Onboarding Buddies']."
        ),
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
        Field(
            description="Sort direction: 'asc' or 'desc'. Only applies when sort='hired_at'."
        ),
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
    show_match = bool(names)
    entries = [
        f"[{i}]\n{_format_person(p, show=show, show_match=show_match)}"
        for i, p in enumerate(data.results, 1)
    ]
    footer = ""
    if names and len(data.results) > 1:
        footer = (
            "\n\nMultiple people matched. Use title/department/location to pick the "
            "right uid, and confirm with the user if the match is ambiguous."
        )
    return header + "\n\n".join(entries) + footer


async def get_profile(
    uid: Annotated[
        int,
        Field(
            description=(
                "Numeric user id of the person whose profile to fetch. Resolve this "
                "with search_people first when you only have a name."
            ),
            ge=1,
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Get a specific person's full GoProfiles profile by uid
    (https://www.goprofiles.io).

    Combines users.php, certifications.php, and languages.php into one normalized
    response with bio, title, languages, skills, certifications, interests, groups,
    and contact info. Only an explicit allow-list of profile fields is returned —
    other fields from the underlying endpoints are stripped.

    Use after search_people when you need the full profile for a resolved uid.
    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    not_found = f"No profile found for uid {uid}. Confirm the uid from search_people and try again."

    user_params = external_params({"uid": uid}, tool="get_profile")
    try:
        user_response = await _api_get(
            "/users.php",
            user_params,
            authorization,
            not_found_message=not_found,
        )
    except LookupError:
        return not_found

    user_raw = user_response.json()
    if not isinstance(user_raw, dict) or not user_raw.get("uid"):
        return not_found

    cert_params = external_params(
        {"uid": uid, "forProfile": "true"}, tool="get_profile"
    )
    lang_params = external_params(
        {"include_uid": uid, "limit": 200, "offset": 0}, tool="get_profile"
    )

    try:
        cert_response, lang_response = await asyncio.gather(
            _api_get(
                "/certifications.php",
                cert_params,
                authorization,
                not_found_message=not_found,
            ),
            _api_get(
                "/languages/index.php",
                lang_params,
                authorization,
                not_found_message=not_found,
            ),
        )
    except LookupError:
        return not_found

    certifications = CertificationsResponse.model_validate(cert_response.json()).results
    languages = LanguagesResponse.model_validate(lang_response.json()).results
    profile = _normalize_profile(user_raw, certifications, languages)
    return _format_profile(profile)
