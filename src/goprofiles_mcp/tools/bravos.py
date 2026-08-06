"""Bravo tools — badge catalog search, draft prepare, and confirmed send."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Annotated, Any

import httpx
from fastmcp import Context
from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    external_params,
    get_authorization_header,
    http_client,
    raise_for_status,
)
from goprofiles_mcp.drafts import put_bravo_draft, take_bravo_draft

# Tokens are only printed by search_bravo_types; prepare_bravo rejects invented ones.
_BADGE_TOKEN_SECRET = os.environ.get(
    "GOPROFILES_MCP_BADGE_TOKEN_SECRET", "goprofiles-mcp-badge-token"
)

BRAVO_PREVIEW_URI = "ui://widget/bravo-preview.html"

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BravoTypeResult(BaseModel):
    bid: int = 0
    name: str = ""
    description: str = ""
    image: str = ""


class BravoTypesResponse(BaseModel):
    results: list[BravoTypeResult] = []


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


def _badge_token(bid: int, name: str) -> str:
    """Opaque token proving bid/name came from search_bravo_types output."""
    payload = f"{bid}\n{name.strip().lower()}".encode()
    digest = hmac.new(
        _BADGE_TOKEN_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return digest[:24]


def _cloudfront_image_url(url: str | None) -> str:
    """Rewrite S3 bucket URLs to CloudFront-style hosts (never s3.amazonaws.com)."""
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw:
        return ""
    # Relative app paths are not usable in the ChatGPT sandbox iframe.
    if raw.startswith(("./", "/")):
        return ""
    prefix = "https://s3.amazonaws.com/"
    if raw.startswith(prefix):
        # https://s3.amazonaws.com/images-dev.goprofiles.io/x -> https://images-dev.goprofiles.io/x
        return "https://" + raw[len(prefix) :]
    http_s3 = "http://s3.amazonaws.com/"
    if raw.startswith(http_s3):
        return "https://" + raw[len(http_s3) :]
    return raw


def _format_bravo_type(b: BravoTypeResult) -> str:
    token = _badge_token(b.bid, b.name)
    lines = [
        f"Name:        {b.name or 'Unknown'}",
        f"Description: {b.description or 'None'}",
        # bid / badge_token are for prepare_bravo only — never show to the user.
        f"bid:         {b.bid}  (tool use only — do not show to the user)",
        f"badge_token: {token}  (tool use only — pass to prepare_bravo; do not show)",
    ]
    return "\n".join(lines)


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


def _validate_badge(
    badges: list[BravoTypeResult], bid: int, badge_name: str
) -> BravoTypeResult | str:
    """Ensure bid exists and badge_name matches the catalog (case-insensitive)."""
    match = next((b for b in badges if b.bid == bid), None)
    if match is None:
        return (
            "Invalid bid: that badge type was not found in this workspace's "
            "catalog. You MUST call search_bravo_types in THIS conversation and "
            "pass a bid (and exact badge name) from that tool's results. Do not "
            "reuse bids or badge names from other chats, memory, or examples. "
            "Do not show bid values to the user."
        )

    if match.name.strip().lower() != badge_name.strip().lower():
        return (
            f"badge_name does not match bid. Catalog name for this bid is "
            f"'{match.name}'. Call search_bravo_types in THIS conversation, then "
            "pass that result's bid together with its exact Name field as "
            "badge_name. Do not invent or reuse names from other chats. Do not "
            "show bid values to the user."
        )

    return match


def _validate_badge_token(bid: int, name: str, badge_token: str) -> str | None:
    """Return an error string if badge_token was not issued for this bid/name."""
    expected = _badge_token(bid, name)
    provided = badge_token.strip().lower()
    if provided and hmac.compare_digest(expected, provided):
        return None
    return (
        "Invalid or missing badge_token. You MUST call search_bravo_types in THIS "
        "conversation first, then pass that result's bid, exact Name as "
        "badge_name, and badge_token together. Do not invent a badge from memory "
        "or other chats. Do not show bid or badge_token to the user."
    )


def bravo_preview_html() -> str:
    """HTML for the ChatGPT / MCP Apps Bravo confirmation widget."""
    return """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="color-scheme" content="light dark" />
  <style>
    :root {
      --bg: #f6f4f1;
      --card: #ffffff;
      --ink: #1c1917;
      --muted: #57534e;
      --accent: #0f766e;
      --accent-ink: #f0fdfa;
      --border: #e7e5e4;
      --danger: #9f1239;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #1c1917;
        --card: #292524;
        --ink: #fafaf9;
        --muted: #a8a29e;
        --accent: #2dd4bf;
        --accent-ink: #042f2e;
        --border: #44403c;
        --danger: #fb7185;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--ink);
      padding: 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      max-width: 420px;
      margin: 0 auto;
      box-shadow: 0 8px 24px rgba(0,0,0,0.06);
    }
    .eyebrow { font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); margin: 0 0 8px; }
    h1 { font-size: 18px; margin: 0 0 4px; }
    .to { color: var(--muted); font-size: 14px; margin: 0 0 16px; }
    .badge {
      display: flex; gap: 14px; align-items: center;
      margin-bottom: 16px; padding: 12px; border-radius: 12px;
      background: color-mix(in srgb, var(--accent) 10%, transparent);
    }
    .badge img {
      width: 72px; height: 72px; object-fit: contain; border-radius: 12px;
      background: var(--card); border: 1px solid var(--border);
    }
    .badge-placeholder {
      width: 72px; height: 72px; border-radius: 12px;
      background: var(--border); display: grid; place-items: center;
      font-size: 11px; color: var(--muted); text-align: center; padding: 6px;
    }
    .badge-name { font-weight: 650; font-size: 16px; }
    .message {
      white-space: pre-wrap; line-height: 1.45; font-size: 15px;
      margin: 0 0 18px; padding: 12px; border-left: 3px solid var(--accent);
    }
    .actions { display: flex; gap: 10px; }
    button {
      flex: 1; border: 0; border-radius: 999px; padding: 12px 16px;
      font-size: 14px; font-weight: 650; cursor: pointer;
    }
    button.send { background: var(--accent); color: var(--accent-ink); }
    button.cancel { background: transparent; color: var(--muted); border: 1px solid var(--border); }
    button:disabled { opacity: 0.55; cursor: wait; }
    .status { margin-top: 12px; font-size: 13px; color: var(--muted); min-height: 1.2em; }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--accent); }
  </style>
</head>
<body>
  <div class="card">
    <p class="eyebrow">Bravo preview</p>
    <h1 id="title">Confirm before sending</h1>
    <p class="to" id="to"></p>
    <div class="badge">
      <div id="image-slot"></div>
      <div>
        <div class="badge-name" id="badge-name"></div>
      </div>
    </div>
    <p class="message" id="message"></p>
    <div class="actions">
      <button class="cancel" id="cancel" type="button">Cancel</button>
      <button class="send" id="send" type="button">Send Bravo</button>
    </div>
    <p class="status" id="status">Review this Bravo, then send or cancel.</p>
  </div>
  <script type="module">
    import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

    const app = new App({ name: "Bravo Preview", version: "1.0.0" });
    let draftId = "";

    function setStatus(text, kind) {
      const el = document.getElementById("status");
      el.textContent = text;
      el.className = "status" + (kind ? " " + kind : "");
    }

    function parsePayload(result) {
      if (result?.structuredContent && typeof result.structuredContent === "object") {
        return result.structuredContent;
      }
      const text = result?.content?.find?.((c) => c.type === "text")?.text
        || (typeof result?.content === "string" ? result.content : "");
      if (!text) return null;
      const marker = "DRAFT_JSON:";
      const idx = text.indexOf(marker);
      if (idx >= 0) {
        try { return JSON.parse(text.slice(idx + marker.length).trim()); }
        catch { return null; }
      }
      return null;
    }

    function render(data) {
      if (!data) {
        setStatus("Waiting for draft data…");
        return;
      }
      draftId = data.draft_id || "";
      document.getElementById("badge-name").textContent = data.badge_name || "Bravo";
      document.getElementById("message").textContent = data.message || "";
      const label = data.recipient_label || "coworker";
      document.getElementById("to").textContent = "To: " + label;
      const slot = document.getElementById("image-slot");
      slot.replaceChildren();
      if (data.image_url) {
        const img = document.createElement("img");
        img.src = data.image_url;
        img.alt = data.badge_name || "Bravo badge";
        slot.appendChild(img);
      } else {
        const ph = document.createElement("div");
        ph.className = "badge-placeholder";
        ph.textContent = "No image";
        slot.appendChild(ph);
      }
      setStatus("Review this Bravo, then send or cancel.");
    }

    app.ontoolresult = (result) => {
      render(parsePayload(result));
    };

    document.getElementById("cancel").onclick = () => {
      setStatus("Cancelled — ask the assistant to prepare a new draft if needed.");
      document.getElementById("send").disabled = true;
    };

    document.getElementById("send").onclick = async () => {
      if (!draftId) {
        setStatus("Missing draft_id — call prepare_bravo again.", "error");
        return;
      }
      const sendBtn = document.getElementById("send");
      sendBtn.disabled = true;
      setStatus("Sending Bravo…");
      try {
        const result = await app.callServerTool({
          name: "send_bravo",
          arguments: { draft_id: draftId },
        });
        const text = result?.content?.find?.((c) => c.type === "text")?.text
          || (typeof result?.content === "string" ? result.content : "Sent.");
        const failed = /not sent|failed|expired|invalid/i.test(text);
        setStatus(text, failed ? "error" : "ok");
        if (!failed) document.getElementById("cancel").disabled = true;
        else sendBtn.disabled = false;
      } catch (err) {
        setStatus("Send failed: " + (err?.message || err), "error");
        sendBtn.disabled = false;
      }
    };

    await app.connect();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search_bravo_types(
    search: Annotated[
        str,
        Field(
            description=(
                "Free-text phrase matched against badge type name and description "
                "(case-insensitive substring). Examples: 'going above and beyond', "
                "'team player', 'collaborat'. Use words that describe the recognition "
                "you want to give."
            ),
            min_length=1,
        ),
    ],
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
    """Search the catalog of giveable Bravo badge types in the user's GoProfiles
    workspace (https://www.goprofiles.io).

    REQUIRED before prepare_bravo in every conversation. Always call this tool in
    the current chat before drafting a Bravo — even if you already "know" a badge
    name like Team Player from another chat, memory, or examples. Badge ids (bid)
    are workspace-specific and must come from this tool's output.

    Returns each badge type's name, description, a numeric 'bid', and a
    badge_token for prepare_bravo. Pass bid, the exact Name as badge_name, and
    badge_token from the same result — prepare_bravo rejects invented badges.

    After results return, list the badge Name(s) to the user and ask which bravo
    type they want — even when there is only one match. Do not call prepare_bravo
    until they explicitly choose or confirm a badge by name.

    The 'bid' and 'badge_token' are internal for prepare_bravo. Never show, read
    aloud, or otherwise expose them to the user; refer to badge types by name.

    Read-only. Requires search:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    badges = await _fetch_bravo_types(authorization, tool="search_bravo_types")

    scored: list[tuple[int, BravoTypeResult]] = []
    for badge in badges:
        score = _match_score(badge, search)
        if score is None:
            continue
        scored.append((score, badge))

    scored.sort(key=lambda item: (item[0], item[1].name.lower()))
    matches = [badge for _, badge in scored[:limit]]

    if not matches:
        return (
            "No bravo badge types matched that search. Matching is substring-based "
            "against name and description — retry with a shorter phrase "
            "(e.g. 'above and beyond' or 'collaborat'), or try a different theme "
            "from the recognition message. Tell the user nothing matched and ask "
            "how they'd like to refine the search."
        )

    total = len(scored)
    names = [b.name or "Unknown" for b in matches]
    name_list = ", ".join(f"'{n}'" for n in names)
    header = f"Bravo badge types ({len(matches)} of {total} matched):\n"
    entries = [f"[{i}]\n{_format_bravo_type(b)}" for i, b in enumerate(matches, 1)]
    footer = (
        "\n\nSTOP — required next step before prepare_bravo:\n"
        f"1. Tell the user these matching bravo type name(s): {name_list}.\n"
        "2. Ask which one they want to use"
        + (
            " (or confirm the single match)."
            if len(matches) == 1
            else ", and wait for their choice."
        )
        + "\n"
        "3. Only after they explicitly choose/confirm a name, call prepare_bravo "
        "with that row's bid, badge_name, badge_token, and the recognition "
        "message.\n"
        "Do not invent or reuse values that did not appear above. Do not show "
        "bid or badge_token to the user. Never call send_bravo yourself — the "
        "user sends from the preview widget after prepare_bravo."
    )
    return header + "\n\n".join(entries) + footer


async def prepare_bravo(
    receiver_uid: Annotated[
        int,
        Field(
            description=(
                "Numeric user id of the recipient from search_people in THIS "
                "conversation. Pass the uid only — never show it to the user. "
                "The giver is always the authenticated user (from the OAuth "
                "token); do not invent or pass a giver uid."
            ),
            ge=1,
        ),
    ],
    bid: Annotated[
        int,
        Field(
            description=(
                "Numeric badge type id from search_bravo_types in THIS "
                "conversation only. Never invent a bid or reuse one from another "
                "chat, memory, or examples. Pass the bid only — never show it to "
                "the user."
            ),
            ge=1,
        ),
    ],
    badge_name: Annotated[
        str,
        Field(
            description=(
                "Exact badge Name string from the same search_bravo_types result "
                "as bid. Do not invent names from memory or other chats."
            ),
            min_length=1,
        ),
    ],
    badge_token: Annotated[
        str,
        Field(
            description=(
                "Opaque badge_token from the same search_bravo_types result as "
                "bid and badge_name. Required. Never show this value to the user."
            ),
            min_length=1,
        ),
    ],
    message: Annotated[
        str | None,
        Field(
            description=(
                "Recognition message for the Bravo (API 'comment', max 850 chars). "
                "Omit when the user has not supplied text yet — ask whether to "
                "draft one or wait for theirs. Never invent and send without asking."
            ),
            max_length=850,
        ),
    ] = None,
    recipient_name: Annotated[
        str | None,
        Field(
            description=(
                "Display name of the recipient from search_people (for the preview "
                "card only). Never invent; omit if unknown."
            ),
        ),
    ] = None,
    ctx: Context | None = None,
) -> ToolResult | str:
    """Prepare a Bravo draft for user confirmation (does NOT send).

    Validates the badge against the live catalog, stores a short-lived draft,
    and returns structured preview data for the confirmation widget. The user
    must click Send in the widget (send_bravo) — never call send_bravo yourself.

    Required workflow:
    1. search_people for receiver_uid (+ recipient_name for display).
    2. search_bravo_types; ask the user which badge name to use.
    3. If no message yet, ask draft-vs-provide.
    4. Call prepare_bravo with the chosen bid/badge_name/badge_token and message.
    5. Show the preview widget; wait for the user to Send or Cancel.

    Read-only. Requires search:read scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    badges = await _fetch_bravo_types(authorization, tool="prepare_bravo")
    resolved = _validate_badge(badges, bid, badge_name)
    if isinstance(resolved, str):
        return resolved

    token_error = _validate_badge_token(resolved.bid, resolved.name, badge_token)
    if token_error is not None:
        return token_error

    if not message or not message.strip():
        return (
            "Message is required before preparing a draft. Ask the user whether "
            "they want you to draft a recognition message for their review, or "
            "provide the text themselves. Do not invent a message without asking. "
            "After the text is ready, call prepare_bravo again with that message "
            "and the same bid/badge_name/badge_token from search_bravo_types."
        )

    message = message.strip()
    image_url = _cloudfront_image_url(resolved.image)
    recipient_label = (recipient_name or "").strip() or "coworker"

    draft_id = put_bravo_draft(
        receiver_uid=receiver_uid,
        bid=resolved.bid,
        badge_name=resolved.name,
        message=message,
        image_url=image_url,
        recipient_label=recipient_label,
    )

    payload: dict[str, Any] = {
        "draft_id": draft_id,
        "recipient_label": recipient_label,
        "badge_name": resolved.name,
        "message": message,
        "image_url": image_url,
    }

    summary = (
        "Bravo draft ready (NOT sent). A confirmation card should appear for the "
        f"user showing badge '{resolved.name}' to {recipient_label}. Ask them to "
        "review the message and click Send in the widget, or Cancel and tell you "
        "what to change. Never call send_bravo yourself — only the widget Send "
        "button should send. Do not show draft_id, uid, bid, or badge_token to "
        "the user.\n"
        f"DRAFT_JSON:{json.dumps(payload, separators=(',', ':'))}"
    )
    return ToolResult(content=summary, structured_content=payload)


async def send_bravo(
    draft_id: Annotated[
        str,
        Field(
            description=(
                "Opaque draft_id from prepare_bravo. This is the only input — "
                "do not pass message, bid, or recipient. Prefer calling this from "
                "the preview widget Send button, not from the model."
            ),
            min_length=1,
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Send a previously prepared Bravo draft by draft_id only.

    Loads the exact payload stored by prepare_bravo (single-use, expires in
    ~5 minutes) and POSTs it to bravos.php. Rejects missing/expired drafts.

    Not read-only. Requires profiles:write scope. Intended for the confirmation
    widget (UI-only visibility); the model should not call this tool.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")

    draft = take_bravo_draft(draft_id)
    if draft is None:
        return (
            "Bravo not sent — draft missing or expired. Call prepare_bravo again "
            "to create a fresh draft, then have the user confirm in the preview."
        )

    authorization = get_authorization_header(ctx)
    params = external_params(tool="send_bravo")

    try:
        response = await http_client.post(
            "/bravos.php",
            params=params,
            data={
                "bid": draft.bid,
                "comment": draft.message,
                "receiver_uids[]": draft.receiver_uid,
            },
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, "/bravos.php")

    data = CreateBravoResponse.model_validate(response.json())

    if data.status == "failed" or data.successful_count == 0:
        detail = data.message.strip() or "The Bravo could not be sent."
        return (
            f"Failed to send Bravo. {detail} Prepare a new draft with "
            "prepare_bravo after confirming the recipient and badge, then try "
            "again from the preview."
        )

    lines = [
        data.message.strip() or "Bravo sent successfully.",
        f"Badge:      {draft.badge_name}",
        f"To:         {draft.recipient_label}",
        f"Successful: {data.successful_count}",
    ]
    if data.failed_count:
        lines.append(f"Failed:     {data.failed_count}")
    if data.scheduled:
        lines.append("Note:       This Bravo was scheduled for later delivery.")
    return "\n".join(lines)

