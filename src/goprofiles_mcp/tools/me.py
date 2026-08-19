"""Identity and reward-points tools for the signed-in (caller's own) user."""

from fastmcp import Context
from pydantic import BaseModel

from goprofiles_mcp.client import (
    api_get,
    external_params,
    get_authorization_header,
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class WhoAmI(BaseModel):
    uid: int = 0
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""


class PointsBalance(BaseModel):
    redeemable_points: int = 0
    # None means unlimited giveable points, not missing data.
    giveable_points: int | None = None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def get_me(ctx: Context | None = None) -> str:
    """Identity of the signed-in user making this request
    (https://www.goprofiles.io): their uid, name, username, and email.

    This is the only way to resolve "me" / "my profile" / "myself" to a uid —
    call it first whenever the user asks about themselves, before reaching for
    search_people (which can only look up other people by name and has no
    notion of "me"). Once you have the uid, follow up with get_profile(uid)
    for the full profile or get_availability(uid) for their own availability.
    For their reward points balance, use get_my_points instead.

    Never show the uid to the user. Read-only. Requires profiles:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    params = external_params({"me": 1}, tool="get_me")
    try:
        response = await api_get(
            "/users.php",
            params,
            authorization,
            not_found_message="Could not resolve the signed-in user from this token.",
        )
    except LookupError as exc:
        return str(exc)

    me = WhoAmI.model_validate(response.json())

    name = f"{me.first_name} {me.last_name}".strip() or me.username or "Unknown"
    lines = [
        f"Name:     {name}",
        f"Username: {me.username or 'Unknown'}",
        f"Email:    {me.email or 'Unknown'}",
        # uid is for follow-up tool calls — never repeat it in user-facing replies.
        f"uid:      {me.uid}  (tool use only — do not show to the user)",
    ]
    return "\n".join(lines)


async def get_my_points(ctx: Context | None = None) -> str:
    """Reward points balance of the signed-in user making this request
    (https://www.goprofiles.io): redeemable points and giveable points.

    Giveable points being unlimited is a normal, expected value for some
    workspaces — report it as "Unlimited", not as missing data. Workspaces
    without the rewards add-on enabled (or a token missing bravos:read) will
    report points as not available; that is also normal, not an error to
    retry.

    Read-only. Requires bravos:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    params = external_params({}, tool="get_my_points")
    try:
        response = await api_get("/points/users.php", params, authorization)
    except PermissionError:
        # The rewards add-on is disabled for this workspace, or the token
        # lacks bravos:read — either way, this is the expected common case,
        # not an error worth aborting the tool for.
        return "Reward points aren't available for this workspace."

    balance = PointsBalance.model_validate(response.json())
    if balance.giveable_points is None:
        giveable = "Unlimited"
    else:
        giveable = str(balance.giveable_points)
    lines = [
        f"Redeemable points: {balance.redeemable_points}",
        f"Giveable points:   {giveable}",
    ]
    return "\n".join(lines)
