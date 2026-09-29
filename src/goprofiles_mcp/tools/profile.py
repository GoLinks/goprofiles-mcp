"""Preview and apply updates to the signed-in user's own profile.

Writable surface (PROF-4729 v1): bio (`intro`), skills, certifications, and
languages. Title/location/contact/interests/custom bio fields are out of scope.

Request body field names match the Part 1 external self-only write endpoints
(PROF-4728). Bodies are form-encoded like the other PHP write tools; uid is
never sent — the API derives the target from the bearer session. If Dockup
smoke shows a different param name, change the constants in `_API` below.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

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

_TOOL = "update_my_profile"

# Form field names / paths for Part 1 self-only profile writes. Centralized so
# a Dockup mismatch is a one-line fix rather than a hunt through the module.
_API = {
    "users": "/users.php",
    "skills": "/skills/user_skills.php",
    "certifications": "/certifications.php",
    "languages": "/languages/users.php",
    "intro": "intro",
    "skill_name": "name",
    "cert_name": "name",
    "cert_issue_date": "issue_date",
    "cert_category": "category",
    "cert_expiration_date": "expiration_date",
    "cert_credential_id": "credential_id",
    "language_code": "code",
    "language_name": "name",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HttpMethod = Literal["PUT", "POST", "DELETE"]


class CertificationAdd(BaseModel):
    name: str = Field(min_length=1)
    issue_date: str = Field(
        description="Issue date as YYYY-MM-DD (required by the certifications API)."
    )
    category: str | None = None
    expiration_date: str | None = None
    credential_id: str | None = None


# ---------------------------------------------------------------------------
# Normalization / confirm-arg builders
# ---------------------------------------------------------------------------


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_str_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = raw.strip()
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _normalize_cert_adds(
    certifications_add: list[CertificationAdd] | None,
) -> list[dict[str, str]]:
    if not certifications_add:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for cert in certifications_add:
        name = cert.name.strip()
        issue_date = cert.issue_date.strip()
        if not name or not issue_date:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, str] = {"name": name, "issue_date": issue_date}
        for field in ("category", "expiration_date", "credential_id"):
            value = getattr(cert, field)
            if isinstance(value, str) and value.strip():
                entry[field] = value.strip()
        out.append(entry)
    return out


def _normalize_lang_list(values: list[str] | None) -> list[str]:
    """Language codes (or bare names) — keep as provided after strip/dedupe."""
    return _clean_str_list(values)


def _build_confirm_args(
    *,
    intro: str | None,
    skills_add: list[str],
    skills_remove: list[str],
    certifications_add: list[dict[str, str]],
    certifications_remove: list[str],
    languages_add: list[str],
    languages_remove: list[str],
) -> dict[str, Any]:
    """Human-readable args the confirming call must resend."""
    args: dict[str, Any] = {}
    if intro is not None:
        args["intro"] = intro
    if skills_add:
        args["skills_add"] = skills_add
    if skills_remove:
        args["skills_remove"] = skills_remove
    if certifications_add:
        args["certifications_add"] = certifications_add
    if certifications_remove:
        args["certifications_remove"] = certifications_remove
    if languages_add:
        args["languages_add"] = languages_add
    if languages_remove:
        args["languages_remove"] = languages_remove
    return args


def _build_ops(confirm_args: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered HTTP ops executed from the staged payload after claim."""
    ops: list[dict[str, Any]] = []

    if "intro" in confirm_args:
        ops.append(
            {
                "method": "PUT",
                "path": _API["users"],
                "body": {_API["intro"]: confirm_args["intro"]},
                "label": "bio",
            }
        )

    for name in confirm_args.get("skills_add", []):
        ops.append(
            {
                "method": "POST",
                "path": _API["skills"],
                "body": {_API["skill_name"]: name},
                "label": f"add skill {name}",
            }
        )
    for name in confirm_args.get("skills_remove", []):
        ops.append(
            {
                "method": "DELETE",
                "path": _API["skills"],
                "body": {_API["skill_name"]: name},
                "label": f"remove skill {name}",
            }
        )

    for cert in confirm_args.get("certifications_add", []):
        body = {
            _API["cert_name"]: cert["name"],
            _API["cert_issue_date"]: cert["issue_date"],
        }
        if "category" in cert:
            body[_API["cert_category"]] = cert["category"]
        if "expiration_date" in cert:
            body[_API["cert_expiration_date"]] = cert["expiration_date"]
        if "credential_id" in cert:
            body[_API["cert_credential_id"]] = cert["credential_id"]
        ops.append(
            {
                "method": "POST",
                "path": _API["certifications"],
                "body": body,
                "label": f"add certification {cert['name']}",
            }
        )
    for name in confirm_args.get("certifications_remove", []):
        ops.append(
            {
                "method": "DELETE",
                "path": _API["certifications"],
                "body": {_API["cert_name"]: name},
                "label": f"remove certification {name}",
            }
        )

    for code in confirm_args.get("languages_add", []):
        ops.append(
            {
                "method": "POST",
                "path": _API["languages"],
                "body": {_API["language_code"]: code},
                "label": f"add language {code}",
            }
        )
    for code in confirm_args.get("languages_remove", []):
        ops.append(
            {
                "method": "DELETE",
                "path": _API["languages"],
                "body": {_API["language_code"]: code},
                "label": f"remove language {code}",
            }
        )

    return ops


def _validate_inputs(
    *,
    intro: str | None,
    skills_add: list[str],
    skills_remove: list[str],
    certifications_add: list[dict[str, str]],
    certifications_remove: list[str],
    languages_add: list[str],
    languages_remove: list[str],
) -> str | None:
    if not any(
        [
            intro is not None,
            skills_add,
            skills_remove,
            certifications_add,
            certifications_remove,
            languages_add,
            languages_remove,
        ]
    ):
        return (
            "No preview — pass at least one change (bio/intro, skills, "
            "certifications, or languages)."
        )

    for cert in certifications_add:
        if not _DATE_RE.match(cert["issue_date"]):
            return (
                f"No preview — certification '{cert['name']}' needs issue_date as "
                "YYYY-MM-DD."
            )
        expiration = cert.get("expiration_date")
        if expiration and not _DATE_RE.match(expiration):
            return (
                f"No preview — certification '{cert['name']}' expiration_date must "
                "be YYYY-MM-DD when set."
            )

    overlap_skills = sorted(
        {s.casefold() for s in skills_add} & {s.casefold() for s in skills_remove}
    )
    if overlap_skills:
        return (
            "No preview — the same skill cannot be both added and removed in one "
            f"update: {', '.join(overlap_skills)}."
        )

    overlap_langs = sorted(
        {s.casefold() for s in languages_add} & {s.casefold() for s in languages_remove}
    )
    if overlap_langs:
        return (
            "No preview — the same language cannot be both added and removed in "
            f"one update: {', '.join(overlap_langs)}."
        )

    add_cert_names = {c["name"].casefold() for c in certifications_add}
    overlap_certs = sorted(
        add_cert_names & {c.casefold() for c in certifications_remove}
    )
    if overlap_certs:
        return (
            "No preview — the same certification cannot be both added and removed "
            f"in one update: {', '.join(overlap_certs)}."
        )

    return None


def _format_preview(confirm_args: dict[str, Any]) -> str:
    lines = ["Profile update previewed — NOT applied.", ""]
    if "intro" in confirm_args:
        lines.append("Bio:")
        lines.append(confirm_args["intro"])
        lines.append("")
    if confirm_args.get("skills_add"):
        lines.append("Skills to add:    " + ", ".join(confirm_args["skills_add"]))
    if confirm_args.get("skills_remove"):
        lines.append("Skills to remove: " + ", ".join(confirm_args["skills_remove"]))
    if confirm_args.get("certifications_add"):
        lines.append("Certifications to add:")
        for cert in confirm_args["certifications_add"]:
            extra = []
            if cert.get("category"):
                extra.append(cert["category"])
            extra.append(f"issued {cert['issue_date']}")
            if cert.get("expiration_date"):
                extra.append(f"expires {cert['expiration_date']}")
            if cert.get("credential_id"):
                extra.append(f"id {cert['credential_id']}")
            lines.append(f"  - {cert['name']} ({'; '.join(extra)})")
    if confirm_args.get("certifications_remove"):
        lines.append(
            "Certifications to remove: "
            + ", ".join(confirm_args["certifications_remove"])
        )
    if confirm_args.get("languages_add"):
        lines.append("Languages to add:    " + ", ".join(confirm_args["languages_add"]))
    if confirm_args.get("languages_remove"):
        lines.append(
            "Languages to remove: " + ", ".join(confirm_args["languages_remove"])
        )

    lines.append("")
    lines.append(
        "NEXT STEP — show the user the changes above and ask them to confirm. "
        "Do not call update_my_profile until they explicitly approve. When they "
        "do, call update_my_profile with the same arguments copied exactly from "
        "this preview. update_my_profile takes no uid; it updates the "
        "authenticated user's profile only."
    )
    return "\n".join(lines)


def _format_summary(confirm_args: dict[str, Any]) -> str:
    bits: list[str] = []
    if "intro" in confirm_args:
        bits.append("update bio")
    if confirm_args.get("skills_add") or confirm_args.get("skills_remove"):
        bits.append("change skills")
    if confirm_args.get("certifications_add") or confirm_args.get(
        "certifications_remove"
    ):
        bits.append("change certifications")
    if confirm_args.get("languages_add") or confirm_args.get("languages_remove"):
        bits.append("change languages")
    what = ", ".join(bits) if bits else "update profile"
    return (
        f"Apply this profile update ({what})?\n\n"
        + _format_preview(confirm_args).split("NEXT STEP")[0].strip()
    )


async def _request(
    method: HttpMethod,
    path: str,
    body: dict[str, Any],
    authorization: str,
) -> httpx.Response:
    params = external_params(tool=_TOOL)
    kwargs: dict[str, Any] = {
        "params": params,
        "headers": {"Authorization": authorization},
        "data": body,
    }
    try:
        if method == "PUT":
            return await http_client.put(path, **kwargs)
        if method == "POST":
            return await http_client.post(path, **kwargs)
        return await http_client.request("DELETE", path, **kwargs)
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")


async def _execute_ops(
    ops: list[dict[str, Any]], authorization: str
) -> tuple[list[str], str | None]:
    """Run staged ops in order. Stop on first failure; return applied labels."""
    applied: list[str] = []
    for op in ops:
        response = await _request(op["method"], op["path"], op["body"], authorization)
        try:
            raise_for_status(response, op["path"])
        except PermissionError as exc:
            return applied, str(exc)
        except (LookupError, ValueError, RuntimeError) as exc:
            return applied, str(exc)
        applied.append(op["label"])
    return applied, None


def _collect_args(
    intro: str | None,
    skills_add: list[str] | None,
    skills_remove: list[str] | None,
    certifications_add: list[CertificationAdd] | None,
    certifications_remove: list[str] | None,
    languages_add: list[str] | None,
    languages_remove: list[str] | None,
) -> tuple[dict[str, Any], str | None]:
    # Empty string intro means clear/set to empty bio; None means omit.
    normalized_intro = None if intro is None else intro.strip()
    skills_add_n = _clean_str_list(skills_add)
    skills_remove_n = _clean_str_list(skills_remove)
    certs_add_n = _normalize_cert_adds(certifications_add)
    certs_remove_n = _clean_str_list(certifications_remove)
    langs_add_n = _normalize_lang_list(languages_add)
    langs_remove_n = _normalize_lang_list(languages_remove)

    error = _validate_inputs(
        intro=normalized_intro if intro is not None else None,
        skills_add=skills_add_n,
        skills_remove=skills_remove_n,
        certifications_add=certs_add_n,
        certifications_remove=certs_remove_n,
        languages_add=langs_add_n,
        languages_remove=langs_remove_n,
    )
    if error:
        return {}, error

    confirm_args = _build_confirm_args(
        intro=normalized_intro if intro is not None else None,
        skills_add=skills_add_n,
        skills_remove=skills_remove_n,
        certifications_add=certs_add_n,
        certifications_remove=certs_remove_n,
        languages_add=langs_add_n,
        languages_remove=langs_remove_n,
    )
    return confirm_args, None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def preview_update_my_profile(
    intro: Annotated[
        str | None,
        Field(
            description=(
                "New bio/intro text for the signed-in user. Omit to leave bio "
                "unchanged. Pass an empty string only when the user explicitly "
                "asked to clear their bio."
            ),
        ),
    ] = None,
    skills_add: Annotated[
        list[str] | None,
        Field(
            description=(
                "Skill names to add to the signed-in user's profile, e.g. "
                "['Python', 'Public speaking']. Omit when not adding skills."
            ),
        ),
    ] = None,
    skills_remove: Annotated[
        list[str] | None,
        Field(
            description=(
                "Skill names to remove from the signed-in user's profile. Copy "
                "names from get_profile when possible. Omit when not removing."
            ),
        ),
    ] = None,
    certifications_add: Annotated[
        list[CertificationAdd] | None,
        Field(
            description=(
                "Certifications to add. Each needs name and issue_date "
                "(YYYY-MM-DD). Optional category, expiration_date, credential_id."
            ),
        ),
    ] = None,
    certifications_remove: Annotated[
        list[str] | None,
        Field(
            description=(
                "Certification names to remove. Copy names from get_profile when "
                "possible. Omit when not removing certifications."
            ),
        ),
    ] = None,
    languages_add: Annotated[
        list[str] | None,
        Field(
            description=(
                "Language codes to add (e.g. ['es', 'fr']). Prefer codes from "
                "get_profile. Omit when not adding languages."
            ),
        ),
    ] = None,
    languages_remove: Annotated[
        list[str] | None,
        Field(
            description=(
                "Language codes to remove. Prefer codes from get_profile. Omit "
                "when not removing languages."
            ),
        ),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Preview an update to the signed-in user's own GoProfiles profile.

    Stages changes to bio (intro), skills, certifications, and/or languages for
    the authenticated user only — there is no uid parameter and no way to edit
    someone else. Nothing is written until update_my_profile runs after the user
    explicitly approves this preview.

    Gather the intended changes, call this once, show the user the preview, and
    wait for approval. Title, location, contact, interests, and custom bio
    fields are not supported.

    Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    # Auth header required even though preview issues no HTTP — keeps the
    # pending write keyed to a real bearer and fails closed without one.
    get_authorization_header(ctx)

    confirm_args, error = _collect_args(
        intro,
        skills_add,
        skills_remove,
        certifications_add,
        certifications_remove,
        languages_add,
        languages_remove,
    )
    if error:
        return error

    ops = _build_ops(confirm_args)
    stage(
        ctx,
        tool=_TOOL,
        payload={"ops": ops},
        confirm_args=confirm_args,
    )
    return _format_preview(confirm_args)


async def update_my_profile(
    intro: Annotated[
        str | None,
        Field(
            description=(
                "Bio/intro exactly as staged by preview_update_my_profile. Omit "
                "only when the preview did not include a bio change."
            ),
        ),
    ] = None,
    skills_add: Annotated[
        list[str] | None,
        Field(
            description=(
                "Skills to add, copied verbatim from the preview. Omit when the "
                "preview listed none."
            ),
        ),
    ] = None,
    skills_remove: Annotated[
        list[str] | None,
        Field(
            description=(
                "Skills to remove, copied verbatim from the preview. Omit when "
                "the preview listed none."
            ),
        ),
    ] = None,
    certifications_add: Annotated[
        list[CertificationAdd] | None,
        Field(
            description=(
                "Certifications to add, copied verbatim from the preview "
                "(same name, issue_date, and optional fields)."
            ),
        ),
    ] = None,
    certifications_remove: Annotated[
        list[str] | None,
        Field(
            description=(
                "Certification names to remove, copied verbatim from the preview."
            ),
        ),
    ] = None,
    languages_add: Annotated[
        list[str] | None,
        Field(
            description=("Language codes to add, copied verbatim from the preview."),
        ),
    ] = None,
    languages_remove: Annotated[
        list[str] | None,
        Field(
            description=("Language codes to remove, copied verbatim from the preview."),
        ),
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Apply a profile update the user already approved via preview_update_my_profile.

    ONLY call this after preview_update_my_profile and after the user has
    explicitly approved that preview. Resend the same arguments from the preview
    — they are checked against the staged write and are not themselves the write
    payload.

    Updates the authenticated user only (no uid parameter). Not read-only: may
    add or remove skills, certifications, and languages. Requires profiles:write
    scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    confirm_args, error = _collect_args(
        intro,
        skills_add,
        skills_remove,
        certifications_add,
        certifications_remove,
        languages_add,
        languages_remove,
    )
    if error:
        # Rephrase preview-oriented validation for the confirm path.
        return error.replace("No preview —", "No update applied —", 1)

    result = await claim(
        ctx,
        tool=_TOOL,
        confirm_args=confirm_args,
        summary=_format_summary(confirm_args),
    )

    if result.status is ClaimStatus.DECLINED:
        return (
            "No update applied — the user declined. Ask what they'd like to "
            "change. The preview is still valid if they only needed a moment; "
            "otherwise call preview_update_my_profile again with the new details."
        )
    if result.status is ClaimStatus.DRIFTED:
        return (
            "No update applied — the arguments do not match the preview. Copy "
            "intro, skills_add, skills_remove, certifications_add, "
            "certifications_remove, languages_add, and languages_remove verbatim "
            "from the preview_update_my_profile result, or call "
            "preview_update_my_profile again if the user wants something "
            "different. The preview is still valid."
        )
    if result.status is ClaimStatus.EXPIRED:
        return (
            "No update applied — the preview expired. Call "
            "preview_update_my_profile again, then re-confirm with the user."
        )
    if not result.ok:
        return (
            "No update applied — there is no profile update waiting to be "
            "applied. It may already have been applied (check before retrying). "
            "Call preview_update_my_profile first, show the user the preview, "
            "and update only after they approve it."
        )

    sending = result.payload or {}
    ops = sending.get("ops") or []
    applied, failure = await _execute_ops(ops, authorization)

    if failure is None:
        lines = ["Profile updated successfully."]
        if applied:
            lines.append("Applied:")
            lines.extend(f"  - {label}" for label in applied)
        return "\n".join(lines)

    if applied:
        return (
            "Profile update partially applied — stopped after an error.\n"
            "Applied:\n"
            + "\n".join(f"  - {label}" for label in applied)
            + f"\nFailed next step: {failure}\n"
            "Tell the user what landed and what did not, then call "
            "preview_update_my_profile again for anything still missing."
        )
    return (
        f"No update applied — GoProfiles rejected the change. {failure} "
        "Tell the user it was not applied, ask what to change, and call "
        "preview_update_my_profile again."
    )
