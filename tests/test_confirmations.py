"""Tests for the shared stage/claim write-confirmation mechanism.

This module is the safety net behind every write tool (create_bravo,
schedule_meeting): a single call must never be able to mutate anything. These
tests exercise the state machine directly rather than through a tool, since
every tool's write path reduces to stage() then claim().
"""

from __future__ import annotations

from goprofiles_mcp import confirmations
from goprofiles_mcp.confirmations import ClaimStatus, claim, stage


def _clear_pending():
    with confirmations._lock:
        confirmations._pending.clear()


class TestStageAndClaimHappyPath:
    async def test_claim_returns_staged_payload(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"name": "Jane"})

        result = await claim(c, tool="t1", confirm_args={"name": "Jane"}, summary="s")

        assert result.status is ClaimStatus.OK
        assert result.ok is True
        assert result.payload == {"x": 1}

    async def test_claim_consumes_the_pending_write(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"name": "Jane"})

        await claim(c, tool="t1", confirm_args={"name": "Jane"}, summary="s")
        second = await claim(c, tool="t1", confirm_args={"name": "Jane"}, summary="s")

        assert second.status is ClaimStatus.NOTHING_PENDING

    async def test_restaging_replaces_the_previous_pending_write(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"name": "Old"})
        stage(c, tool="t1", payload={"x": 2}, confirm_args={"name": "New"})

        stale = await claim(c, tool="t1", confirm_args={"name": "Old"}, summary="s")
        fresh = await claim(c, tool="t1", confirm_args={"name": "New"}, summary="s")

        assert stale.status is ClaimStatus.DRIFTED
        assert fresh.status is ClaimStatus.OK
        assert fresh.payload == {"x": 2}


class TestNothingPending:
    async def test_claim_with_no_prior_stage_reports_nothing_pending(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        result = await claim(c, tool="never-staged", confirm_args={}, summary="s")
        assert result.status is ClaimStatus.NOTHING_PENDING
        assert result.ok is False


class TestDrift:
    async def test_mismatched_confirm_args_drift(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"amount": 10})

        result = await claim(c, tool="t1", confirm_args={"amount": 11}, summary="s")

        assert result.status is ClaimStatus.DRIFTED

    async def test_drifted_claim_leaves_the_pending_write_usable(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"amount": 10})

        await claim(c, tool="t1", confirm_args={"wrong": True}, summary="s")
        retry = await claim(c, tool="t1", confirm_args={"amount": 10}, summary="s")

        assert retry.status is ClaimStatus.OK

    async def test_whitespace_rewrapping_is_not_treated_as_drift(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"comment": "hello   world"})

        result = await claim(
            c, tool="t1", confirm_args={"comment": "hello\nworld"}, summary="s"
        )

        assert result.status is ClaimStatus.OK

    async def test_nested_dict_and_list_values_are_compared_deeply(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(
            c,
            tool="t1",
            payload={},
            confirm_args={"items": [{"a": "x  y"}], "meta": {"b": "p  q"}},
        )

        matching = await claim(
            c,
            tool="t1",
            confirm_args={"items": [{"a": "x y"}], "meta": {"b": "p q"}},
            summary="s",
        )
        assert matching.status is ClaimStatus.OK


class TestExpiry:
    async def test_expired_pending_write_reports_expired(self, make_ctx, monkeypatch):
        _clear_pending()
        c = make_ctx()

        times = iter([1000.0, 1000.0, 2000.0, 2000.0])
        monkeypatch.setattr(confirmations.time, "time", lambda: next(times))

        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1}, ttl_seconds=5)
        result = await claim(c, tool="t1", confirm_args={"a": 1}, summary="s")

        assert result.status is ClaimStatus.EXPIRED

    async def test_expired_entry_is_removed_from_the_store(self, make_ctx, monkeypatch):
        _clear_pending()
        c = make_ctx()

        times = iter([1000.0, 1000.0, 2000.0, 2000.0])
        monkeypatch.setattr(confirmations.time, "time", lambda: next(times))

        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1}, ttl_seconds=5)
        await claim(c, tool="t1", confirm_args={"a": 1}, summary="s")

        assert confirmations._pending == {}


class TestDeclined:
    async def test_claim_declined_via_elicitation(self, make_ctx):
        _clear_pending()
        c = make_ctx(supports_elicitation=True, elicit_response=None)
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1})

        result = await claim(c, tool="t1", confirm_args={"a": 1}, summary="Confirm?")

        assert result.status is ClaimStatus.DECLINED

    async def test_declined_claim_leaves_the_pending_write_usable(self, make_ctx):
        _clear_pending()
        c = make_ctx(supports_elicitation=True, elicit_response=None)
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1})

        await claim(c, tool="t1", confirm_args={"a": 1}, summary="Confirm?")

        c.session.supports_elicitation = False
        retry = await claim(c, tool="t1", confirm_args={"a": 1}, summary="Confirm?")
        assert retry.status is ClaimStatus.OK

    async def test_claim_accepted_via_elicitation(self, make_ctx):
        from fastmcp.server.elicitation import AcceptedElicitation

        _clear_pending()
        c = make_ctx(
            supports_elicitation=True,
            elicit_response=AcceptedElicitation(data="Confirm"),
        )
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1})

        result = await claim(c, tool="t1", confirm_args={"a": 1}, summary="Confirm?")

        assert result.status is ClaimStatus.OK

    async def test_claim_with_no_elicitation_capability_fails_open(self, make_ctx):
        """Fires without asking when the client offers no elicitation capability
        (e.g. ChatGPT today) — the structural two-call handshake is the only
        gate in that case, by design."""
        _clear_pending()
        c = make_ctx(supports_elicitation=False)
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1})

        result = await claim(c, tool="t1", confirm_args={"a": 1}, summary="Confirm?")

        assert result.status is ClaimStatus.OK


class TestPerCallerIsolation:
    async def test_different_bearer_tokens_get_independent_pending_writes(
        self, make_ctx
    ):
        _clear_pending()
        alice = make_ctx(authorization="Bearer alice-token")
        bob = make_ctx(authorization="Bearer bob-token")

        stage(alice, tool="t1", payload={"who": "alice"}, confirm_args={"a": 1})

        bob_claim = await claim(bob, tool="t1", confirm_args={"a": 1}, summary="s")
        alice_claim = await claim(alice, tool="t1", confirm_args={"a": 1}, summary="s")

        assert bob_claim.status is ClaimStatus.NOTHING_PENDING
        assert alice_claim.status is ClaimStatus.OK

    async def test_different_tools_hold_independent_pending_writes(self, make_ctx):
        _clear_pending()
        c = make_ctx()
        stage(c, tool="create_bravo", payload={"a": 1}, confirm_args={"k": 1})
        stage(c, tool="schedule_meeting", payload={"b": 2}, confirm_args={"k": 1})

        bravo = await claim(c, tool="create_bravo", confirm_args={"k": 1}, summary="s")
        meeting = await claim(
            c, tool="schedule_meeting", confirm_args={"k": 1}, summary="s"
        )

        assert bravo.payload == {"a": 1}
        assert meeting.payload == {"b": 2}

    async def test_stdio_transport_falls_back_to_session_id(self, make_ctx):
        _clear_pending()
        c = make_ctx(no_request_context=True, session_id="session-abc")
        stage(c, tool="t1", payload={"x": 1}, confirm_args={"a": 1})

        result = await claim(c, tool="t1", confirm_args={"a": 1}, summary="s")

        assert result.status is ClaimStatus.OK
