"""Short-lived, single-use in-memory drafts for prepare/send confirmation."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

# Default TTL matches the plan's 5-minute draft window.
DEFAULT_DRAFT_TTL_SECONDS = 5 * 60


@dataclass
class BravoDraft:
    receiver_uid: int
    bid: int
    badge_name: str
    message: str
    image_url: str
    recipient_label: str
    expires_at: float


_lock = threading.Lock()
_drafts: dict[str, BravoDraft] = {}


def put_bravo_draft(
    *,
    receiver_uid: int,
    bid: int,
    badge_name: str,
    message: str,
    image_url: str = "",
    recipient_label: str = "",
    ttl_seconds: int = DEFAULT_DRAFT_TTL_SECONDS,
) -> str:
    """Store a draft and return its opaque draft_id."""
    draft_id = uuid.uuid4().hex
    draft = BravoDraft(
        receiver_uid=receiver_uid,
        bid=bid,
        badge_name=badge_name,
        message=message,
        image_url=image_url,
        recipient_label=recipient_label,
        expires_at=time.time() + ttl_seconds,
    )
    with _lock:
        _purge_expired_unlocked()
        _drafts[draft_id] = draft
    return draft_id


def take_bravo_draft(draft_id: str) -> BravoDraft | None:
    """Pop a draft if present and not expired (single-use)."""
    with _lock:
        _purge_expired_unlocked()
        return _drafts.pop(draft_id.strip(), None)


def _purge_expired_unlocked() -> None:
    now = time.time()
    expired = [key for key, draft in _drafts.items() if draft.expires_at < now]
    for key in expired:
        del _drafts[key]
