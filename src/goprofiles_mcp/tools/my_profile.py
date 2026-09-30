"""The signed-in user's own profile: read it, and edit it via preview → confirm.

We use the same endpoint as the web profile page saves through, so the rules match the web app
when it comes to updating profile fields.
"""

from __future__ import annotations

import difflib
import html
import re
import urllib.parse
from typing import Annotated, Any

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

_TOOL = "update_my_profile"
_PATH = "/users.php"
_DEPARTMENTS_PATH = "/departments/index.php"

_BIRTHDAY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_COUNTRY_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_NO_BIRTHDAY = "00-00"

# ---------------------------------------------------------------------------
# Pydantic models — the profile as the tools see it
# ---------------------------------------------------------------------------


class EditableField(BaseModel):
    label: str = ""
    value: Any = None
    editable: bool = False
    locked_reason: str | None = None
    locked_by: str | None = None
    timezone: str | None = None
    iso2: str | None = None
    options: list[str] | None = None


class CustomField(BaseModel):
    ufid: int = 0
    name: str = ""
    field_type: str = ""
    instructions: str | None = None
    example: str | None = None
    options: list[str] | None = None
    value: str | list[str] | None = None
    editable: bool = False
    locked_reason: str | None = None
    iso2: str | None = None
    url_name: str | None = None


class EditableProfile(BaseModel):
    fields: dict[str, EditableField] = {}
    custom_fields: list[CustomField] = []


# ---------------------------------------------------------------------------
# Pydantic models — tool input
# ---------------------------------------------------------------------------


class LocationChange(BaseModel):
    city: str = Field(default="", description="City, e.g. 'Austin'. May be empty.")
    state: str = Field(
        default="", description="State, province, or region, e.g. 'Texas'."
    )
    country: str = Field(
        default="", description="Country name, e.g. 'United States'. Include it."
    )


class CustomFieldChange(BaseModel):
    name: str = Field(
        description="The custom field's name exactly as get_my_profile lists it."
    )
    value: str | list[str] = Field(
        description=(
            "New value. A string for text-like and dropdown fields (a dropdown value "
            "must be one of its listed options). A list of option strings for "
            "multi choice fields. '' or [] clears it."
        )
    )


class ProfileChanges(BaseModel):
    """Only the fields to change. Omit (null) a field to leave it as is; pass ''
    to clear it."""

    first_name: str | None = None
    last_name: str | None = None
    title: str | None = Field(default=None, description="Job title.")
    department: str | None = Field(
        default=None,
        description=(
            "Department name as the user gave it. The preview matches it to an "
            "existing department (ignoring case and spacing) or flags that it would "
            "create a new one and suggests close matches. Cannot be cleared."
        ),
    )
    pronouns: str | None = Field(default=None, description="Max 20 characters.")
    bio: str | None = Field(default=None, description="About-me text, max 300 chars.")
    work_phone: str | None = Field(
        default=None,
        description=(
            "Local number without the international calling code, e.g. "
            "'512 555 0100'. Put the country in work_phone_country instead of a "
            "leading '+'."
        ),
    )
    work_phone_country: str | None = Field(
        default=None,
        description="Two-letter country code for work_phone, e.g. 'US', 'GB'.",
    )
    personal_phone: str | None = Field(
        default=None,
        description=(
            "Local number without the international calling code; put the country "
            "in personal_phone_country."
        ),
    )
    personal_phone_country: str | None = Field(
        default=None,
        description="Two-letter country code for personal_phone.",
    )
    birthday: str | None = Field(
        default=None, description="Month and day as MM-DD, e.g. '07-14'. No year."
    )
    location: LocationChange | None = Field(
        default=None,
        description=(
            "Where the user is based. It is looked up on a map, which also sets "
            "their timezone, so always include the country. Pass all three parts "
            "empty to clear it."
        ),
    )
    linkedin: str | None = None
    twitter: str | None = Field(default=None, description="X (Twitter) handle.")
    github: str | None = None
    personal_website: str | None = Field(default=None, description="A URL.")
    custom_fields: list[CustomFieldChange] | None = Field(
        default=None, description="Workspace-specific bio fields to change."
    )


# ProfileChanges attribute
_FIELD_MAP: dict[str, tuple[str, str]] = {
    "first_name": ("first_name", "First name"),
    "last_name": ("last_name", "Last name"),
    "title": ("title", "Job title"),
    "department": ("department", "Department"),
    "pronouns": ("pronouns", "Pronouns"),
    "bio": ("intro", "Bio"),
    "work_phone": ("phone", "Work phone"),
    "personal_phone": ("personal_phone", "Personal phone"),
    "birthday": ("birthday", "Birthday"),
    "location": ("location", "Location"),
    "linkedin": ("linkedin", "LinkedIn"),
    "twitter": ("twitter", "X (Twitter)"),
    "github": ("github", "GitHub"),
    "personal_website": ("personal_website", "Personal website"),
}
_COUNTRY_MAP = {
    "work_phone_country": ("work_phone", "phone_iso2"),
    "personal_phone_country": ("personal_phone", "personal_phone_iso2"),
}
_PARAM_BY_API_KEY = {api: param for param, (api, _) in _FIELD_MAP.items()}
_LABEL_BY_PARAM = {param: label for param, (_, label) in _FIELD_MAP.items()}

_MAX_LENGTHS = {
    "first_name": 100,
    "last_name": 100,
    "title": 100,
    "department": 100,
    "pronouns": 20,
    "bio": 300,
    "linkedin": 100,
    "twitter": 50,
    "github": 100,
    "personal_website": 200,
}
_PHONE_REGEX = re.compile(r"^[0-9().\-\s]+$")

# ---------------------------------------------------------------------------
# users.php → EditableProfile
# ---------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    """Department names are stored HTML-escaped; show them the way the web does.
    Other values are stored as typed and are sent back
    verbatim, so they must not be unescaped."""
    if value is None:
        return None
    return html.unescape(str(value))


def _profile_from_user(user: dict[str, Any], departments: list[str]) -> EditableProfile:
    """Build the tools' view of a users.php ?me response.

    Editability comes only from the server's `editable_fields`; a field it does not
    report is treated as locked so a server that predates it fails closed.
    """
    locks: dict[str, Any] = user.get("editable_fields") or {}

    def field(key: str, value: Any, **extra: Any) -> EditableField:
        lock = locks.get(key) or {}
        return EditableField(
            label=_LABEL_BY_PARAM[_PARAM_BY_API_KEY[key]],
            value=value,
            editable=lock.get("editable") is True,
            locked_reason=lock.get("locked_reason"),
            locked_by=lock.get("locked_by"),
            **extra,
        )

    birthday = user.get("birthday")
    fields = {
        "first_name": field("first_name", user.get("first_name")),
        "last_name": field("last_name", user.get("last_name")),
        "title": field("title", user.get("title")),
        "department": field(
            "department", _text(user.get("department")), options=departments
        ),
        "pronouns": field("pronouns", user.get("pronouns")),
        "intro": field("intro", user.get("intro")),
        "phone": field("phone", user.get("phone"), iso2=user.get("phone_iso2")),
        "personal_phone": field(
            "personal_phone",
            user.get("personal_phone"),
            iso2=user.get("personal_phone_iso2"),
        ),
        "birthday": field(
            "birthday", None if birthday in (None, "", _NO_BIRTHDAY) else birthday
        ),
        "location": field(
            "location",
            {k: user.get(k) for k in ("city", "state", "country")},
            timezone=user.get("timezone"),
        ),
        "linkedin": field("linkedin", user.get("linkedin")),
        "twitter": field("twitter", user.get("twitter")),
        "github": field("github", user.get("github")),
        "personal_website": field("personal_website", user.get("personal_website")),
    }

    bio_lock = locks.get("bio_fields") or {}
    custom_fields = []
    for c in user.get("bio_fields") or []:
        field_type = c.get("field_type") or ""
        if field_type in ("dropdown", "multi choice"):
            selected = list(c.get("selected") or [])
            value: str | list[str] | None = (
                selected if field_type == "multi choice" else (selected or [None])[0]
            )
            options = list(c.get("options") or [])
        else:
            value = c.get("value")
            options = None
        custom_fields.append(
            CustomField(
                ufid=c.get("ufid") or 0,
                name=_text(c.get("name")) or "",
                field_type=field_type,
                instructions=c.get("instructions"),
                example=c.get("example"),
                options=options,
                value=value,
                editable=bio_lock.get("editable") is True,
                locked_reason=bio_lock.get("locked_reason"),
                iso2=c.get("iso2"),
                url_name=c.get("url_name"),
            )
        )
    return EditableProfile(fields=fields, custom_fields=custom_fields)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _department_key(name: str) -> str:
    """Mirror of the API's Helpers::convertToNameID — the key it matches
    departments on, so 'customer  success' and 'Customer Success' are the same."""
    lowered = re.sub(r"[^a-z0-9\s]", " ", html.unescape(name).lower())
    return "-".join(lowered.split())


def _lock_text(reason: str | None, locked_by: str | None) -> str:
    if reason == "synced":
        return f"synced from {locked_by or 'an external data source'} — change it there"
    if reason == "admin_managed":
        return "only a workspace admin can change this"
    if reason == "disabled":
        return "turned off for this workspace"
    return "GoProfiles does not allow changing this here"


def _location_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "None"
    parts = [
        str(value.get(k)).strip()
        for k in ("city", "state", "country")
        if value.get(k) and str(value.get(k)).strip()
    ]
    return ", ".join(parts) or "None"


def _value_text(key: str, f: EditableField) -> str:
    if key == "location":
        text = _location_text(f.value)
        return f"{text} (timezone {f.timezone})" if f.timezone else text
    if f.value in (None, ""):
        return "None"
    if key in ("phone", "personal_phone") and f.iso2:
        return f"{f.value} ({f.iso2})"
    return str(f.value)


def _custom_value_text(value: str | list[str] | None) -> str:
    if isinstance(value, list):
        return ", ".join(value) or "None"
    return value or "None"


def _custom_field_heading(c: CustomField) -> str:
    detail = c.field_type or "text"
    if c.options:
        detail += f"; options: {', '.join(c.options)}"
    return f"{c.name} ({detail})"


def _format_profile(profile: EditableProfile) -> str:
    editable: list[str] = []
    locked: list[str] = []
    for key, f in profile.fields.items():
        param = _PARAM_BY_API_KEY.get(key, key)
        line = f"  {f.label or key} [{param}]: {_value_text(key, f)}"
        if f.editable:
            editable.append(line)
        else:
            locked.append(f"{line} — {_lock_text(f.locked_reason, f.locked_by)}")

    lines = ["Your GoProfiles profile (the signed-in user).", "", "Editable:"]
    lines += editable or ["  None"]
    lines += ["", "Locked (cannot be changed from here):"]
    lines += locked or ["  None"]

    lines += ["", "Custom bio fields:"]
    if not profile.custom_fields:
        lines.append("  None")
    for c in profile.custom_fields:
        line = f"  {_custom_field_heading(c)}: {_custom_value_text(c.value)}"
        if not c.editable:
            line += f" — {_lock_text(c.locked_reason, None)}"
        elif c.instructions:
            line += f"\n      Hint: {c.instructions}"
        lines.append(line)
    return "\n".join(lines)


def _confirm_summary(confirm_args: dict[str, Any]) -> str:
    """The approval prompt: the requested values, labelled like the preview."""
    lines = ["Save these changes to your GoProfiles profile?", ""]
    for param, value in confirm_args.items():
        if param == "custom_fields":
            for change in value:
                lines.append(f"{change['name']}: {_custom_value_text(change['value'])}")
        elif param == "location":
            lines.append(f"Location: {_location_text(value)}")
        elif param in _COUNTRY_MAP:
            phone_param = _COUNTRY_MAP[param][0]
            lines.append(f"{_LABEL_BY_PARAM[phone_param]} country: {value}")
        elif param in ("work_phone", "personal_phone") and value:
            digits = re.sub(r"\D", "", value)
            lines.append(f"{_LABEL_BY_PARAM[param]}: {digits}")
        elif param == "department":
            lines.append(
                f"Department: {value} (created for the whole workspace if it "
                "doesn't exist yet)"
            )
        else:
            lines.append(f"{_LABEL_BY_PARAM[param]}: {value or '(cleared)'}")
    return "\n".join(lines)


def _form_fields(body: dict[str, Any]) -> dict[str, str]:
    """Flatten a PUT body into PHP-style form keys (`bio_fields_text[0][ufid]`),
    which is what users.php parses with parse_str()."""
    flat: dict[str, str] = {}

    def add(key: str, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                add(f"{key}[{k}]", v)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                add(f"{key}[{i}]", v)
        else:
            flat[key] = "" if value is None else str(value)

    for key, value in body.items():
        add(key, value)
    return flat


def _api_error_message(response: httpx.Response) -> str | None:
    """users.php's own error message, or None for an OAuth error body."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict):
        # {"error": "insufficient_scope", ...} — an auth failure, not a refusal.
        return None
    return error.get("message") or f"HTTP {response.status_code}"


_URL_SCHEMES = (
    "http",
    "https",
    "mailto",
    "slack",
    "msteams",
    "zoommtg",
    "notion",
    "figma",
)


def _website_url(value: str) -> str | None:
    """The URL users.php would store for `value`, or None if it would reject it."""
    if not re.match(rf"^({'|'.join(_URL_SCHEMES)}):", value, re.IGNORECASE):
        value = f"https://{value}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() in ("http", "https") and (
        not parsed.netloc or re.search(r"\s", value)
    ):
        return None
    return value


# ---------------------------------------------------------------------------
# Change resolution
# ---------------------------------------------------------------------------


class _Refusal(Exception):
    """A change that must not be staged; the message is for the model/user."""


class _Resolved(BaseModel):
    body: dict[str, Any]
    lines: list[str]
    new_department: str | None = None
    custom_field_names: dict[int, str] = {}


def _resolve_changes(changes: ProfileChanges, profile: EditableProfile) -> _Resolved:
    """Validate requested changes against the live profile and build the PUT body.

    Returns the users.php body plus preview lines ("Label: old → new"). Raises
    _Refusal for anything locked, unknown, or malformed so nothing is staged.
    Values are checked the way users.php checks them, because a value it rejects
    can fail after other changes were already written.
    """
    requested = changes.model_dump(exclude_none=True)
    body: dict[str, Any] = {}
    lines: list[str] = []
    notes: list[str] = []
    locked: list[str] = []
    new_department: str | None = None

    for param, (api_key, label) in _FIELD_MAP.items():
        if param not in requested:
            continue
        current = profile.fields.get(api_key, EditableField(label=label))
        if not current.editable:
            locked.append(
                f"{label} — {_lock_text(current.locked_reason, current.locked_by)}"
            )
            continue

        new = requested[param]
        if param == "location":
            new = {k: (new.get(k) or "").strip() for k in ("city", "state", "country")}
            if any(new.values()) and not new["country"]:
                raise _Refusal(
                    "Include the country with the location — it is looked up on a "
                    "map, and a city alone can match the wrong place."
                )
            body.update(new)
            new_text = _location_text(new)
            old_text = _location_text(current.value)
            lines.append(f"{label}: {old_text} → {new_text}")
            continue

        new = new.strip()
        new_text = new or "(cleared)"
        old_text = _value_text(api_key, current)
        if param == "first_name" and not new:
            raise _Refusal("First name cannot be empty.")
        if param == "department" and not new:
            raise _Refusal("Department cannot be cleared from here.")
        if param == "birthday" and new and not _BIRTHDAY_RE.match(new):
            raise _Refusal("birthday must be MM-DD, e.g. '07-14'.")
        if param in _MAX_LENGTHS and len(new) > _MAX_LENGTHS[param]:
            raise _Refusal(
                f"{label} can be at most {_MAX_LENGTHS[param]} characters "
                f"(this is {len(new)})."
            )
        if param in ("work_phone", "personal_phone") and new:
            if new.startswith("+"):
                raise _Refusal(
                    f"Give {param} without the '+' calling code and set "
                    f"{param}_country to the two-letter country instead."
                )
            digits = re.sub(r"\D", "", new)
            if not _PHONE_REGEX.match(new) or not digits:
                raise _Refusal(f"{label} can only contain digits, spaces, -, . and ().")
            new = new_text = digits
        if param == "personal_website" and new:
            url = _website_url(new)
            if url is None:
                raise _Refusal(f"'{new}' is not a valid URL.")
            new = new_text = url

        body[api_key] = _NO_BIRTHDAY if param == "birthday" and not new else new
        line = f"{label}: {old_text} → {new_text}"
        if param == "department":
            existing = {_department_key(o): o for o in current.options or []}
            if _department_key(new) in existing:
                # Show the stored spelling — that's what the profile will read.
                line = f"{label}: {old_text} → {existing[_department_key(new)]}"
            else:
                new_department = new
                line += "  (NEW — this creates a department in the workspace)"
                similar = difflib.get_close_matches(
                    new, current.options or [], n=3, cutoff=0.6
                )
                notes.append(
                    f"'{new}' is not an existing department, so saving creates it "
                    "for the whole workspace. Point this out to the user"
                    + (
                        f" and ask whether they meant {' or '.join(similar)}"
                        if similar
                        else ""
                    )
                    + "."
                )
        lines.append(line)

    # users.php keeps the stored name unless both parts arrive together.
    for pair, other in (("first_name", "last_name"), ("last_name", "first_name")):
        if pair in body and other not in body:
            body[other] = profile.fields.get(other, EditableField()).value or ""

    for param, (phone_param, api_key) in _COUNTRY_MAP.items():
        if param not in requested:
            continue
        code = requested[param].strip().upper()
        if not _COUNTRY_CODE_RE.match(code):
            raise _Refusal(f"{param} must be a two-letter country code, e.g. 'US'.")
        if phone_param not in requested:
            raise _Refusal(
                f"{param} can only be changed together with {phone_param} — pass "
                "the phone number as well."
            )
        body[api_key] = code
        lines.append(f"{_FIELD_MAP[phone_param][1]} country: {code}")

    by_name = {c.name.casefold(): c for c in profile.custom_fields}
    names: dict[int, str] = {}
    for change in changes.custom_fields or []:
        field = by_name.get(change.name.strip().casefold())
        if field is not None and field.ufid in names:
            raise _Refusal(f"{field.name} is listed more than once.")
        if field is None:
            known = ", ".join(c.name for c in profile.custom_fields) or "none"
            raise _Refusal(
                f"There is no custom field named '{change.name}'. Available: {known}."
            )
        if not field.editable:
            locked.append(f"{field.name} — {_lock_text(field.locked_reason, None)}")
            continue
        value = change.value
        if field.field_type == "multi choice":
            if not isinstance(value, list):
                value = [value] if value.strip() else []
            value = [v.strip() for v in value if v.strip()]
            bad = [v for v in value if v not in (field.options or [])]
            # An empty list sends no `selected` key, which users.php reads as
            # "deselect all".
            body.setdefault("bio_fields_multi_choice", []).append(
                {"ufid": field.ufid, "selected": value}
            )
        else:
            if isinstance(value, list):
                raise _Refusal(f"{field.name} takes a single value, not a list.")
            value = value.strip()
            if field.field_type == "dropdown":
                bad = [value] if value and value not in (field.options or []) else []
                body.setdefault("bio_fields_dropdown", []).append(
                    {"ufid": field.ufid, "selection": value}
                )
            else:
                bad = []
                entry: dict[str, Any] = {"ufid": field.ufid, "value": value}
                if field.field_type == "phone" and field.iso2:
                    entry["iso2"] = field.iso2
                if field.field_type == "url" and field.url_name:
                    entry["url_name"] = field.url_name
                body.setdefault("bio_fields_text", []).append(entry)
        if bad:
            raise _Refusal(
                f"{', '.join(repr(b) for b in bad)} is not an option for "
                f"{field.name}. Options: {', '.join(field.options or [])}."
            )
        names[field.ufid] = field.name
        lines.append(
            f"{field.name}: {_custom_value_text(field.value)} → "
            f"{_custom_value_text(value) if value else '(cleared)'}"
        )

    if locked:
        raise _Refusal(
            "These can't be changed from here because of your workspace's "
            "settings:\n" + "\n".join(f"  - {item}" for item in locked)
        )
    if not lines:
        raise _Refusal("No changes were given.")

    if notes:
        lines += [""] + [f"Note: {n}" for n in notes]
    return _Resolved(
        body=body,
        lines=lines,
        new_department=new_department,
        custom_field_names=names,
    )


async def _fetch_profile(
    authorization: str, *, tool: str, with_departments: bool = False
) -> EditableProfile:
    """The signed-in user's profile. `with_departments` also loads the workspace's
    department names, which only a department change needs (to match or flag it)."""
    response = await api_get(
        _PATH, external_params({"me": "true"}, tool=tool), authorization
    )
    user = response.json()

    departments: list[str] = []
    department_editable = (
        (user.get("editable_fields") or {}).get("department", {}).get("editable")
    )
    if with_departments and department_editable:
        dept_response = await api_get(
            _DEPARTMENTS_PATH, external_params(tool=tool), authorization
        )
        departments = list(
            dict.fromkeys(
                _text(d["name"])
                for d in dept_response.json().get("results") or []
                if d.get("name")
            )
        )
    return _profile_from_user(user, departments)


def _failed_bio_lines(failed: Any, names: dict[int, str]) -> list[str]:
    """users.php's failed_bio_updates ({"text": [...], ...}, or [] when none)."""
    if not isinstance(failed, dict):
        return []
    lines = []
    for entries in failed.values():
        for entry in entries or []:
            ufid = int(entry.get("ufid") or 0)
            name = names.get(ufid, f"custom field {ufid}")
            lines.append(f"  - {name}: {entry.get('error') or 'not saved'}")
    return lines


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def get_my_profile(ctx: Context | None = None) -> str:
    """Get the signed-in user's OWN GoProfiles profile (https://www.goprofiles.io)
    and which parts of it they can edit.

    Use this whenever the user asks about or wants to change "my profile". Unlike
    get_profile it needs no uid and no search_people lookup — it always returns
    the person who is signed in.

    Each field is listed as editable or locked. Locked fields are controlled by
    the workspace: synced from an HR system or directory (edit it there), managed
    by an admin, or turned off. Workspace-specific custom bio fields are listed
    with their type and, for dropdown / multi choice, the allowed options.
    The [name] next to each field is the preview_my_profile_update parameter.

    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)
    return _format_profile(await _fetch_profile(authorization, tool="get_my_profile"))


async def preview_my_profile_update(
    changes: Annotated[
        ProfileChanges,
        Field(
            description=(
                "Only the fields the user asked to change. Leave every other field "
                "out. Use the user's own words for values; never invent a bio, "
                "title, or other content they did not ask for or approve."
            )
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Preview a change to the signed-in user's own GoProfiles profile. Changes
    nothing.

    Checks each requested change against the user's current profile and the
    workspace's data matching settings, and returns a before → after preview.
    If any requested field is locked (synced from an HR system, admin-managed,
    or turned off), nothing is staged and the reason is returned — tell the user
    where that field is managed instead of retrying.

    Only the signed-in user's profile can be changed; there is no way to edit
    anyone else. Call get_my_profile first if you need field names, custom field
    options, or current values. If you draft text for the user (e.g. a bio),
    show it to them before calling this.

    Then show the user the preview and wait for them to explicitly approve it.
    Only after that, call update_my_profile with the same `changes`.

    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    profile = await _fetch_profile(
        authorization,
        tool="preview_my_profile_update",
        with_departments=changes.department is not None,
    )
    try:
        resolved = _resolve_changes(changes, profile)
    except _Refusal as refusal:
        return f"No preview — {refusal}"

    stage(
        ctx,
        tool=_TOOL,
        # Executed as staged: custom fields are already resolved to ids here, so
        # the confirming call has nothing that can redirect the write.
        payload={
            "body": resolved.body,
            "new_department": resolved.new_department,
            "custom_field_names": resolved.custom_field_names,
        },
        confirm_args=changes.model_dump(exclude_none=True),
    )

    return (
        "Profile update previewed — NOT saved.\n\n"
        + "\n".join(resolved.lines)
        + "\n\nNEXT STEP — show the user the changes above and ask them to confirm. "
        "Do not call update_my_profile until they explicitly say to save them. "
        "When they do, call update_my_profile with exactly the same `changes` you "
        "passed here — any difference is refused."
    )


async def update_my_profile(
    changes: Annotated[
        ProfileChanges,
        Field(
            description=(
                "Exactly the same `changes` object you passed to "
                "preview_my_profile_update. It is checked against the preview, "
                "not applied directly."
            )
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Save a change to the signed-in user's own profile that they already
    approved.

    ONLY call this after preview_my_profile_update and after the user has
    explicitly approved that preview. The user asking for a change is not
    approval of the preview — they must see and approve the before → after first.

    Saves the changes staged by the preview for the signed-in user only. The
    workspace's data matching settings are re-checked when saving, so a field an
    admin locked in the meantime is refused. Custom bio fields save one by one:
    the result lists any that did not save.

    Not read-only: overwrites the previous values. Requires profiles:write scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    confirm_args = changes.model_dump(exclude_none=True)
    result = await claim(
        ctx,
        tool=_TOOL,
        confirm_args=confirm_args,
        summary=_confirm_summary(confirm_args),
    )

    if result.status is ClaimStatus.DECLINED:
        return (
            "No changes saved — the user declined. Ask what they'd like to change. "
            "The preview is still valid if they only needed a moment; otherwise "
            "call preview_my_profile_update again."
        )
    if result.status is ClaimStatus.DRIFTED:
        return (
            "No changes saved — `changes` does not match the preview. Pass exactly "
            "the same `changes` you gave preview_my_profile_update, or preview "
            "again if the user wants something different. The preview is still "
            "valid."
        )
    if result.status is ClaimStatus.EXPIRED:
        return (
            "No changes saved — the preview expired. Call preview_my_profile_update "
            "again, then re-confirm with the user."
        )
    if not result.ok:
        return (
            "No changes saved — there is no profile update waiting. It may already "
            "have been saved (check with get_my_profile before retrying). Call "
            "preview_my_profile_update first and save only after the user approves."
        )

    staged = result.payload or {}
    body: dict[str, Any] = staged["body"]
    try:
        response = await http_client.put(
            _PATH,
            params=external_params(tool=_TOOL),
            data=_form_fields(body),
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    # A users.php refusal carries {"error": {"message": ...}}, so we relay it. A
    # 403 is always auth (the only 403 refusal, the external-key guard, can't be
    # hit from here), so it raises and the client can re-authorize.
    may_have_written = bool(
        staged.get("new_department")
        or {"bio_fields_text", "bio_fields_dropdown", "bio_fields_multi_choice"}
        & body.keys()
    )
    relayed = (400, 404, 422, 500) if may_have_written else (400, 404, 422)
    if response.status_code in relayed:
        message = _api_error_message(response)
        if message is not None:
            if not may_have_written:
                return (
                    f"No changes saved — GoProfiles refused the update: {message}. "
                    "Tell the user, and preview again if they want to adjust it."
                )
            return (
                f"Profile may be partly updated — GoProfiles refused the update: "
                f"{message}. Custom bio field changes or a new department may "
                "already have been saved before the refusal. Call get_my_profile "
                "to see what is saved now, tell the user, and preview again if "
                "they want to adjust it."
            )
    raise_for_status(response, _PATH)

    data = response.json()
    failed = _failed_bio_lines(
        data.get("failed_bio_updates"), staged.get("custom_field_names") or {}
    )
    lines = ["Profile updated." if not failed else "Profile partly updated."]
    if staged.get("new_department"):
        lines.append(f"Created new department: {staged['new_department']}")
    if failed:
        lines += ["These custom bio fields were NOT saved:", *failed]
        lines.append("Everything else in the preview was saved. Tell the user.")

    if "city" in body:
        try:
            saved = await _fetch_profile(authorization, tool=_TOOL)
        except (
            PermissionError,
            LookupError,
            RuntimeError,
            TimeoutError,
            ConnectionError,
            ValueError,
        ):
            lines.append(
                "Couldn't re-read the saved location; check with get_my_profile."
            )
        else:
            location = saved.fields.get("location")
            if location is not None:
                lines.append(f"Location saved as: {_value_text('location', location)}")
    return "\n".join(lines)
