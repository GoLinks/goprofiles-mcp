"""Shared write-confirmation mechanism.

Every write tool routes through this so a mutation always takes two calls: one
that stages the intended write, and one that resubmits the same arguments to
actually perform it. A single call can never mutate anything.

Pending writes are keyed to the caller, not handed back as a token. The entry's
existence is itself the proof that this caller staged one, so there is nothing
for the model to carry between calls, nothing it can forge or replay, and
nothing opaque for a host to surface in an approval dialog.

Two mechanisms put a human in the loop, in order of strength:

1. ``user_confirms`` elicits an explicit Confirm/Cancel from clients that
   declare the elicitation capability. This fires even when the host has the
   tool allowlisted, so it is the only true gate.
2. Otherwise the handshake is structural — the model cannot write in one call —
   backed by the host's own write-approval prompt. Tools should resubmit their
   real arguments on the second call rather than the token alone, so that prompt
   shows the user what they are approving instead of an opaque id.

Known limits, both deliberate:

- On a client offering neither elicitation nor a UI surface (ChatGPT today),
  nothing stops a model from issuing both calls in one turn. ``user_confirms``
  returns True there rather than blocking the feature outright.
- Pending writes live in this process's memory. That matches the deployment,
  which is already single-task because MCP session state has the same
  constraint (see ``__main__``). A redeploy drops staged writes; the TTL is
  short and the caller is told to re-stage.
- Only one pending write per caller per tool. Staging again replaces the
  previous one, so a fresh preview always supersedes an abandoned one.
- Under ``MCP_STATELESS=true`` this store still works, but ``ctx.elicit`` does
  not — that flag's documented cost — leaving only the structural gate.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastmcp import Context
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.types import ClientCapabilities, ElicitationCapability

DEFAULT_TTL_SECONDS = 5 * 60


class ClaimStatus(Enum):
    """Why a claim succeeded or failed, so callers can explain it usefully."""

    OK = "ok"
    NOTHING_PENDING = "nothing_pending"  # never staged, already used, or purged
    EXPIRED = "expired"
    DRIFTED = "drifted"  # arguments differ from what was staged
    DECLINED = "declined"  # user was asked and said no


@dataclass
class PendingWrite:
    payload: dict[str, Any]  # what to execute
    confirm_args: dict[str, Any]  # what the caller must resubmit
    expires_at: float


@dataclass
class ClaimResult:
    status: ClaimStatus
    payload: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status is ClaimStatus.OK


logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: dict[str, PendingWrite] = {}


def _redact(key: str) -> str:
    """A key fragment safe to log — never the credential itself."""
    source, _, tool = key.partition("\n")
    kind, _, value = source.partition(":")
    return f"{kind}:{value[:8]}…/{tool}"


def _normalize(value: Any) -> Any:
    """Collapse whitespace in strings so re-wrapping isn't treated as drift."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _purge_expired_unlocked(now: float) -> None:
    for key in [k for k, p in _pending.items() if p.expires_at < now]:
        del _pending[key]


def _owner_key(ctx: Context, tool: str) -> str:
    """Identify who a pending write belongs to.

    Scoping to the caller is what removes the need for a token: the existence of
    an entry under this key already proves this caller staged one, so there is
    nothing for the model to carry between the two calls — and nothing to leak
    into a host's approval dialog.

    Over HTTP this is derived from the **bearer token only**. An earlier version
    preferred the ``mcp-session-id`` header and fell back to the bearer, which
    broke in ChatGPT: it does not hold one MCP session across tool calls, so
    ``preview`` and ``send`` derived different keys and the send always reported
    nothing pending. Mixing two sources is worse than either alone — if the
    header is present on one call and absent on the next, the key changes prefix
    and can never match. The bearer is the caller's actual identity and is
    stable across transport sessions and under ``MCP_STATELESS=true``.

    Only stdio / in-memory transports, which carry no HTTP request, fall back to
    ``ctx.session_id`` (stable for the life of that connection).

    Including the tool lets different write tools hold independent pending
    writes for the same caller.
    """
    request = ctx.request_context.request if ctx.request_context else None
    if request is not None:
        auth = request.headers.get("authorization", "").strip()
        if auth:
            # Hash the credential itself, not the whole header, so a difference
            # in scheme casing or spacing can't change the key.
            token = auth.split(maxsplit=1)[-1]
            digest = hashlib.sha256(token.encode()).hexdigest()
            return f"bearer:{digest}\n{tool}"
    return f"session:{ctx.session_id}\n{tool}"


def stage(
    ctx: Context,
    *,
    tool: str,
    payload: dict[str, Any],
    confirm_args: dict[str, Any],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Record a pending write for this caller, replacing any earlier one.

    ``payload`` is what will be executed. ``confirm_args`` is what the caller
    will resubmit on the confirming call, and is the only thing compared.

    Keeping them separate lets a tool execute on resolved internal ids while
    confirming on human-readable values — so the host's approval prompt shows
    the user a name rather than a number, and the confirming call has no
    parameter capable of redirecting the write.

    Only one pending write per caller per tool: staging again replaces it, so a
    re-preview always supersedes whatever came before.
    """
    now = time.time()
    with _lock:
        _purge_expired_unlocked(now)
        _pending[_owner_key(ctx, tool)] = PendingWrite(
            payload=payload,
            confirm_args=_normalize(confirm_args),
            expires_at=now + ttl_seconds,
        )


def _validate_unlocked(
    key: str, *, confirm_args: dict[str, Any], now: float
) -> ClaimResult:
    pending = _pending.get(key)
    if pending is None:
        # Fires only on a miss, so it's silent in normal use. If a caller's key
        # is not stable across its two calls this is the evidence that says so:
        # compare the attempted key against the ones actually held.
        logger.warning(
            "confirmations: no pending write for %s; currently holding %s",
            _redact(key),
            [_redact(k) for k in _pending] or "nothing",
        )
        return ClaimResult(ClaimStatus.NOTHING_PENDING)
    if pending.expires_at < now:
        del _pending[key]
        return ClaimResult(ClaimStatus.EXPIRED)
    if _normalize(confirm_args) != pending.confirm_args:
        return ClaimResult(ClaimStatus.DRIFTED)
    return ClaimResult(ClaimStatus.OK, payload=pending.payload)


async def claim(
    ctx: Context,
    *,
    tool: str,
    confirm_args: dict[str, Any],
    summary: str,
) -> ClaimResult:
    """Find this caller's pending write, confirm it, and consume it — in order.

    Returns the staged ``payload``, which is what the caller should act on. The
    resubmitted ``confirm_args`` are only ever compared, never executed.

    The entry is consumed **only** on a fully approved claim. Drifted args, a
    declined confirmation, or a crash mid-flight all leave it usable so a
    corrected retry works. Lookup, confirmation, and consumption live in one
    call precisely so a caller cannot get that ordering wrong: confirming after
    consuming would discard the pending write on every decline.
    """
    key = _owner_key(ctx, tool)

    with _lock:
        result = _validate_unlocked(key, confirm_args=confirm_args, now=time.time())
    if not result.ok:
        return result

    if not await user_confirms(ctx, summary=summary):
        return ClaimResult(ClaimStatus.DECLINED)

    # Re-validate under the lock before consuming: the elicitation above is an
    # await, so the entry could have expired or been consumed while we waited.
    with _lock:
        result = _validate_unlocked(key, confirm_args=confirm_args, now=time.time())
        if result.ok:
            del _pending[key]
    return result


async def user_confirms(ctx: Context, *, summary: str) -> bool:
    """Ask the user to approve a write, when the client can be asked at all.

    Returns True when the client declares no elicitation capability — see the
    module docstring for why this fails open rather than blocking the feature.
    """
    if not ctx.session.check_client_capability(
        ClientCapabilities(elicitation=ElicitationCapability())
    ):
        return True

    result = await ctx.elicit(
        summary,
        # An explicit response_type is required: response_type=None is
        # deprecated in FastMCP 3.x and renders an empty form in some clients.
        response_type=["Confirm", "Cancel"],
        response_title="Confirm",
    )
    return isinstance(result, AcceptedElicitation) and result.data == "Confirm"
