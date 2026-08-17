from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from goprofiles_mcp import confirmations
from goprofiles_mcp.confirmations import stage
from goprofiles_mcp.tools.availability import CalendarEvent
from goprofiles_mcp.tools.meetings import (
    _TOOL,
    Attendee,
    ConflictCheck,
    ProviderProbe,
    _check_conflicts,
    _conflict_lines,
    _default_description,
    _duration_line,
    _duration_phrase,
    _end_of_their_day,
    _location,
    _overlaps,
    _person_block,
    _profile_url,
    _rejection,
    _relative,
    _resolve_start_at,
    _when_line,
    preview_meeting,
    schedule_meeting,
)

# Well within a year of "today" and never in the past, regardless of when the
# suite runs.
FUTURE_ISO = "2026-09-01T14:00:00+00:00"


def _clear_pending():
    with confirmations._lock:
        confirmations._pending.clear()


def _attendee_json(**overrides) -> dict:
    base = {
        "uid": 42,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "timezone": "UTC",
        "title": "Engineer",
        "city": "London",
        "state": None,
    }
    base.update(overrides)
    return base


def _stage_ready_meeting(
    ctx,
    *,
    starts_at: str = FUTURE_ISO,
    duration_minutes: int = 30,
    title: str = "Sync up",
    description: str = "Hello there",
    uid: int = 42,
    invite_path: str = "/google-calendar/schedule_meeting.php",
    provider: str = "Google Calendar",
    attendee_name: str = "Ada Lovelace",
    ttl_seconds: int | None = None,
) -> int:
    """Stage a pending write shaped exactly as preview_meeting would have."""
    epoch, _, _ = _resolve_start_at(starts_at)
    kwargs = {"ttl_seconds": ttl_seconds} if ttl_seconds is not None else {}
    stage(
        ctx,
        tool=_TOOL,
        payload={
            "uid_to_meet": uid,
            "starting_time": epoch,
            "meeting_duration_min": duration_minutes,
            "title": title,
            "description": description,
            "invite_path": invite_path,
            "provider": provider,
        },
        confirm_args={
            "attendee_name": attendee_name,
            "starts_at": epoch,
            "duration_minutes": duration_minutes,
            "title": title,
            "description": description,
        },
        **kwargs,
    )
    return epoch


class TestRelative:
    def test_sub_minute_rounds_up_to_one_but_still_pluralizes(self):
        # minutes=0 clamps the displayed number to 1 but the plural check still
        # compares against the raw 0, so this reads as "1 minutes".
        assert _relative(30) == "about 1 minutes from now"

    def test_exactly_one_minute_is_singular(self):
        assert _relative(60) == "about 1 minute from now"

    def test_several_minutes_is_plural(self):
        assert _relative(300) == "about 5 minutes from now"

    def test_one_hour_is_singular(self):
        assert _relative(3600) == "about 1 hour from now"

    def test_several_hours_is_plural(self):
        assert _relative(7200) == "about 2 hours from now"

    def test_48_hours_crosses_into_days(self):
        assert _relative(48 * 3600) == "about 2 days from now"

    def test_several_days_is_plural(self):
        assert _relative(72 * 3600) == "about 3 days from now"


class TestResolveStartAt:
    def test_blank_string_asks_for_a_start_time(self):
        epoch, zone, error = _resolve_start_at("   ")
        assert epoch == 0
        assert zone is None
        assert "no start time was given" in error

    def test_unparseable_string_is_rejected(self):
        epoch, zone, error = _resolve_start_at("whenever works")
        assert epoch == 0
        assert zone is None
        assert "is not a valid date and time" in error

    def test_naive_datetime_defaults_to_pacific(self):
        future_naive = (datetime.now(UTC) + timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        _epoch, zone, error = _resolve_start_at(future_naive)
        assert error is None
        assert zone.key == "America/Los_Angeles"

    def test_past_time_is_rejected(self):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        epoch, zone, error = _resolve_start_at(past)
        assert epoch == 0
        assert zone is None
        assert "is in the past" in error

    def test_more_than_a_year_ahead_is_rejected(self):
        far = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        epoch, zone, error = _resolve_start_at(far)
        assert epoch == 0
        assert zone is None
        assert "more than a year away" in error

    def test_valid_future_time_resolves_epoch_and_offset(self):
        epoch, zone, error = _resolve_start_at("2026-09-01T14:00:00-07:00")
        assert error is None
        assert zone.utcoffset(None) == timedelta(hours=-7)
        assert epoch == int(
            datetime.fromisoformat("2026-09-01T14:00:00-07:00").timestamp()
        )


class TestWhenLine:
    def test_same_day_uses_bare_end_time(self):
        epoch = int(datetime(2026, 8, 20, 10, 0, tzinfo=UTC).timestamp())
        line = _when_line(epoch, 30, UTC)
        assert "Thu 20 Aug 2026, 10:00–10:30 (UTC+0000)" in line
        assert "from now" in line

    def test_crossing_midnight_spells_out_the_end_date(self):
        epoch = int(datetime(2026, 8, 20, 23, 30, tzinfo=UTC).timestamp())
        line = _when_line(epoch, 90, UTC)
        assert "Thu 20 Aug 2026, 23:30–Fri 21 Aug 2026, 01:00 (UTC+0000)" in line


class TestDurationLine:
    def test_hours_only(self):
        assert _duration_line(120) == "2 hours"

    def test_minutes_only(self):
        assert _duration_line(45) == "45 minutes"

    def test_hours_and_minutes(self):
        assert _duration_line(90) == "1 hour 30 minutes"

    def test_zero_minutes(self):
        assert _duration_line(0) == "0 minutes"

    def test_singular_hour(self):
        assert _duration_line(60) == "1 hour"

    def test_singular_minute(self):
        assert _duration_line(1) == "1 minute"


class TestDurationPhrase:
    def test_zero_hours(self):
        assert _duration_phrase(45) == "45 minute"

    def test_zero_minutes(self):
        assert _duration_phrase(120) == "2 hour"

    def test_hours_and_minutes(self):
        assert _duration_phrase(90) == "1 hour 30 minute"


class TestLocation:
    def test_both_present(self):
        assert _location("London", "UK") == "London, UK"

    def test_only_city(self):
        assert _location("London", None) == "London"

    def test_only_state(self):
        assert _location(None, "UK") == "UK"

    def test_neither_present(self):
        assert _location(None, None) is None


class TestProfileUrl:
    def test_builds_the_profile_link(self):
        assert _profile_url("ada") == "https://www.goprofiles.io/profile?username=ada"


class TestPersonBlock:
    # Every field here is someone else's own profile data, not text the caller
    # of the tool wrote — so an unescaped title/name/username would let a
    # person inject markup into every invite anyone books with them, and a raw
    # quote in the username would let it break out of the href attribute
    # entirely. Both calendar APIs render at least some HTML in the
    # description, so this is the only thing standing between a profile field
    # and markup injection in someone else's calendar invite.
    def test_malicious_name_and_title_are_escaped(self):
        attendee = Attendee(
            name="<script>alert(1)</script>",
            title='"><script>x</script>',
            username="",
        )
        block = _person_block(attendee, line_break="\n")
        assert "<script>" not in block
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block
        assert "&quot;&gt;&lt;script&gt;x&lt;/script&gt;" in block

    def test_malicious_username_cannot_break_out_of_the_href_attribute(self):
        attendee = Attendee(name="Jane", username='mal"icious<b>')
        block = _person_block(attendee, line_break="\n")
        assert (
            'href="https://www.goprofiles.io/profile?username='
            'mal&quot;icious&lt;b&gt;"' in block
        )
        assert "<b>" not in block

    @pytest.mark.parametrize("line_break", ["\n", "<br>"])
    def test_lines_are_joined_with_the_given_separator(self, line_break):
        attendee = Attendee(
            name="Ada",
            title="Engineer",
            city="London",
            state="UK",
            username="ada",
        )
        block = _person_block(attendee, line_break=line_break)
        lines = block.split(line_break)
        assert lines == [
            "👤 Ada, Engineer",
            "📍 Located in London, UK",
            (
                '🔗 View profile: <a href="https://www.goprofiles.io/profile?'
                'username=ada">https://www.goprofiles.io/profile?username=ada</a>'
            ),
        ]

    def test_omits_title_location_and_link_lines_when_absent(self):
        attendee = Attendee(name="Ada")
        block = _person_block(attendee, line_break="\n")
        assert block == "👤 Ada"


class TestDefaultDescription:
    def test_outlook_wraps_in_html_paragraphs(self):
        attendee = Attendee(name="Ada Lovelace", title="Engineer", username="ada")
        desc = _default_description(attendee, 30, "Outlook Calendar")
        assert desc.startswith("<p>You've been invited to a 30 minute meeting</p>")
        assert _person_block(attendee, line_break="<br>") in desc
        assert 'Booked on <a href="https://www.goprofiles.io"' in desc

    def test_google_uses_plain_text_wrapping(self):
        attendee = Attendee(name="Ada Lovelace", title="Engineer", username="ada")
        desc = _default_description(attendee, 30, "Google Calendar")
        assert desc.startswith("You've been invited to a 30 minute meeting\n\n")
        assert "<p>" not in desc
        assert _person_block(attendee, line_break="\n") in desc

    def test_both_providers_share_the_same_escaped_content(self):
        attendee = Attendee(name="<b>Ada</b>", username="ada")
        outlook = _default_description(attendee, 30, "Outlook Calendar")
        google = _default_description(attendee, 30, "Google Calendar")
        assert "&lt;b&gt;Ada&lt;/b&gt;" in outlook
        assert "&lt;b&gt;Ada&lt;/b&gt;" in google
        assert "<b>Ada</b>" not in outlook
        assert "<b>Ada</b>" not in google


class TestEndOfTheirDay:
    def test_no_timezone_is_unknowable(self):
        assert _end_of_their_day(None) is None

    def test_invalid_iana_name_degrades_to_unknown(self):
        assert _end_of_their_day("Not/AZone") is None

    def test_valid_timezone_returns_midnight_tomorrow(self):
        zone = ZoneInfo("America/New_York")
        result = _end_of_their_day("America/New_York")
        expected_date = (datetime.now(zone) + timedelta(days=1)).date()
        got = datetime.fromtimestamp(result, tz=zone)
        assert got.date() == expected_date
        assert (got.hour, got.minute, got.second) == (0, 0, 0)


class TestOverlaps:
    def test_touching_end_to_start_is_not_a_conflict(self):
        event = CalendarEvent(start_time=100, end_time=200)
        assert _overlaps(event, 200, 300) is False

    def test_touching_start_to_end_is_not_a_conflict(self):
        event = CalendarEvent(start_time=200, end_time=300)
        assert _overlaps(event, 100, 200) is False

    def test_genuine_overlap_is_a_conflict(self):
        event = CalendarEvent(start_time=150, end_time=250)
        assert _overlaps(event, 100, 200) is True


def _probe(**overrides) -> ProviderProbe:
    base = {
        "label": "Google Calendar",
        "invite_path": "/google-calendar/schedule_meeting.php",
        "outcome": "ok",
        "linked": True,
        "events": [],
        "ooo": None,
    }
    base.update(overrides)
    return ProviderProbe(**base)


def _attendee(**overrides) -> Attendee:
    base = {"name": "Ada", "timezone": "UTC"}
    base.update(overrides)
    return Attendee(**base)


class TestCheckConflicts:
    def test_probe_not_ok_is_unchecked(self):
        check = _check_conflicts(_probe(outcome="on"), _attendee(), 1_000, 30, None)
        assert check.state == "unchecked"
        assert "could not be read" in check.detail

    def test_unlinked_calendar_is_unchecked(self):
        check = _check_conflicts(_probe(linked=False), _attendee(), 1_000, 30, None)
        assert check.state == "unchecked"
        assert "have not connected their Google Calendar" in check.detail

    def test_ooo_overlapping_the_slot_is_a_conflict_regardless_of_horizon(self):
        # No timezone means the horizon is unknowable, yet the OOO check still
        # fires — it looks two months ahead independent of the day-horizon math.
        ooo = CalendarEvent(start_time=5_000, end_time=9_000)
        probe = _probe(ooo=ooo)
        check = _check_conflicts(probe, _attendee(timezone=None), 6_000, 30, None)
        assert check.state == "conflict"
        assert "OUT OF OFFICE" in check.detail

    def test_same_day_clashing_event_is_a_conflict(self):
        slot_start = int(datetime.now(UTC).timestamp()) + 300
        clash = CalendarEvent(start_time=slot_start - 100, end_time=slot_start + 100)
        probe = _probe(events=[clash])
        check = _check_conflicts(probe, _attendee(timezone="UTC"), slot_start, 30, None)
        assert check.state == "conflict"
        assert "already booked" in check.detail

    def test_all_day_event_is_a_conflict_even_without_timestamp_overlap(self):
        slot_start = int(datetime.now(UTC).timestamp()) + 300
        all_day = CalendarEvent(start_time=0, end_time=1, is_all_day=True)
        probe = _probe(events=[all_day])
        check = _check_conflicts(probe, _attendee(timezone="UTC"), slot_start, 30, None)
        assert check.state == "conflict"
        assert "all day" in check.detail

    def test_no_timezone_on_attendee_is_unchecked_with_unknown_horizon(self):
        slot_start = int(datetime.now(UTC).timestamp()) + 300
        check = _check_conflicts(
            _probe(), _attendee(timezone=None), slot_start, 30, None
        )
        assert check.state == "unchecked"
        assert "no timezone" in check.detail

    def test_slot_entirely_past_horizon_is_unchecked(self):
        horizon = _end_of_their_day("UTC")
        slot_start = horizon + 100
        check = _check_conflicts(
            _probe(), _attendee(timezone="UTC"), slot_start, 30, None
        )
        assert check.state == "unchecked"
        assert "past the end of their current day" in check.detail
        assert "after midnight" not in check.detail

    def test_slot_straddling_midnight_is_unchecked_with_different_wording(self):
        horizon = _end_of_their_day("UTC")
        slot_start = horizon - 100
        check = _check_conflicts(
            _probe(), _attendee(timezone="UTC"), slot_start, 10, None
        )
        assert check.state == "unchecked"
        assert "runs past the end of their current day" in check.detail
        assert "after midnight" in check.detail

    def test_clear_slot_with_nothing_on_the_calendar(self):
        slot_start = int(datetime.now(UTC).timestamp()) + 300
        check = _check_conflicts(
            _probe(), _attendee(timezone="UTC"), slot_start, 30, None
        )
        assert check.state == "clear"
        assert "nothing" in check.detail


class TestConflictLines:
    def test_conflict_state_warns_and_calls_out_advisory_only(self):
        check = ConflictCheck(state="conflict", detail="they are already booked")
        line = _conflict_lines(check)
        assert "WARNING — they are already booked." in line
        assert "advisory, not a block" in line

    def test_clear_state(self):
        check = ConflictCheck(state="clear", detail="nothing clashes")
        line = _conflict_lines(check)
        assert "None found — nothing clashes." in line

    def test_unchecked_state_warns_against_reporting_free(self):
        check = ConflictCheck(state="unchecked", detail="could not be checked")
        line = _conflict_lines(check)
        assert "NOT CHECKED — could not be checked." in line
        assert "Do not tell the user this person is free" in line


class TestRejection:
    def test_404_message(self):
        msg = _rejection(404)
        assert "attendee may no longer exist" in msg

    def test_400_message(self):
        msg = _rejection(400)
        assert "signed-in user" in msg

    def test_other_status_message(self):
        msg = _rejection(500)
        assert "calendar provider rejected the event" in msg


class TestPreviewMeeting:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await preview_meeting(
                uid=1,
                starts_at=FUTURE_ISO,
                duration_minutes=30,
                title="Sync",
                description=None,
                ctx=None,
            )

    async def test_blank_title_is_refused(self, api_mock, ctx):
        result = await preview_meeting(
            uid=1,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="   ",
            description=None,
            ctx=ctx,
        )
        assert "the title is empty" in result

    async def test_invalid_starts_at_is_refused_without_staging(self, api_mock, ctx):
        _clear_pending()
        route = api_mock.get("/users.php")

        result = await preview_meeting(
            uid=1,
            starts_at="not-a-date",
            duration_minutes=30,
            title="Sync",
            description=None,
            ctx=ctx,
        )

        assert "is not a valid date and time" in result
        assert route.call_count == 0
        assert confirmations._pending == {}

    async def test_past_starts_at_is_refused(self, api_mock, ctx):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        result = await preview_meeting(
            uid=1,
            starts_at=past,
            duration_minutes=30,
            title="Sync",
            description=None,
            ctx=ctx,
        )
        assert "is in the past" in result

    async def test_too_far_ahead_starts_at_is_refused(self, api_mock, ctx):
        far = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        result = await preview_meeting(
            uid=1,
            starts_at=far,
            duration_minutes=30,
            title="Sync",
            description=None,
            ctx=ctx,
        )
        assert "more than a year away" in result

    async def test_unknown_uid_404_is_refused(self, api_mock, ctx):
        _clear_pending()
        api_mock.get("/users.php").mock(return_value=httpx.Response(404))
        google = api_mock.get("/google-calendar")

        result = await preview_meeting(
            uid=999,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync",
            description=None,
            ctx=ctx,
        )

        assert "no person found with that uid" in result
        assert google.call_count == 0
        assert confirmations._pending == {}

    async def test_unknown_uid_200_with_no_uid_field_is_refused(self, api_mock, ctx):
        _clear_pending()
        api_mock.get("/users.php").mock(return_value=httpx.Response(200, json={}))

        result = await preview_meeting(
            uid=999,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync",
            description=None,
            ctx=ctx,
        )

        assert "no person found with that uid" in result
        assert confirmations._pending == {}

    async def test_no_calendar_integration_connected_is_refused(self, api_mock, ctx):
        _clear_pending()
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json())
        )
        api_mock.get("/google-calendar").mock(return_value=httpx.Response(403))
        api_mock.get("/outlook-calendar").mock(return_value=httpx.Response(403))

        result = await preview_meeting(
            uid=42,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync",
            description=None,
            ctx=ctx,
        )

        assert "no calendar integration connected" in result
        assert confirmations._pending == {}

    async def test_enabled_provider_stages_resolved_payload_and_default_description(
        self, api_mock, ctx
    ):
        _clear_pending()
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json())
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "events": [], "ooo": []}
            )
        )
        api_mock.get("/outlook-calendar").mock(return_value=httpx.Response(403))

        result = await preview_meeting(
            uid=42,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description=None,
            ctx=ctx,
        )

        epoch, _, _ = _resolve_start_at(FUTURE_ISO)
        attendee = Attendee(
            name="Ada Lovelace",
            timezone="UTC",
            title="Engineer",
            city="London",
            state=None,
            username="ada",
        )
        expected_description = _default_description(attendee, 30, "Google Calendar")

        assert len(confirmations._pending) == 1
        pending = next(iter(confirmations._pending.values()))
        assert pending.payload == {
            "uid_to_meet": 42,
            "starting_time": epoch,
            "meeting_duration_min": 30,
            "title": "Sync up",
            "description": expected_description,
            "invite_path": "/google-calendar/schedule_meeting.php",
            "provider": "Google Calendar",
        }

        for label in (
            "With:",
            "When:",
            "Duration:",
            "Calendar:",
            "Title:",
            "Conflicts:",
            "Description:",
        ):
            assert label in result
        assert expected_description in result

    async def test_supplied_description_overrides_the_default(self, api_mock, ctx):
        _clear_pending()
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json())
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "events": [], "ooo": []}
            )
        )
        api_mock.get("/outlook-calendar").mock(return_value=httpx.Response(403))

        await preview_meeting(
            uid=42,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Let's catch up",
            ctx=ctx,
        )

        pending = next(iter(confirmations._pending.values()))
        assert pending.payload["description"] == "Let's catch up"

    async def test_conflicts_line_reflects_check_conflicts_output(self, api_mock, ctx):
        _clear_pending()
        near_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        epoch, _, _ = _resolve_start_at(near_future)
        clash = {
            "title": "busy",
            "start_time": epoch - 600,
            "end_time": epoch + 600,
            "is_all_day": False,
        }
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json(timezone="UTC"))
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "events": [clash], "ooo": []}
            )
        )
        api_mock.get("/outlook-calendar").mock(return_value=httpx.Response(403))

        result = await preview_meeting(
            uid=42,
            starts_at=near_future,
            duration_minutes=30,
            title="Sync up",
            description=None,
            ctx=ctx,
        )

        assert "WARNING" in result
        assert "already booked" in result
        assert "advisory, not a block" in result


class TestScheduleMeeting:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await schedule_meeting(
                attendee_name="Ada Lovelace",
                starts_at=FUTURE_ISO,
                duration_minutes=30,
                title="Sync up",
                description="Hello there",
                ctx=None,
            )

    async def test_nothing_pending_reports_no_meeting_waiting_and_issues_no_post(
        self, api_mock, ctx
    ):
        _clear_pending()
        route = api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Hello there",
            ctx=ctx,
        )

        assert "no meeting waiting to be scheduled" in result
        assert route.call_count == 0

    async def test_drifted_args_report_mismatch_and_issue_no_post(self, api_mock, ctx):
        _clear_pending()
        _stage_ready_meeting(ctx, title="Original title")
        route = api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="A different title",
            description="Hello there",
            ctx=ctx,
        )

        assert "do not match the preview" in result
        assert route.call_count == 0

    async def test_declined_confirmation_issues_no_post(self, api_mock, make_ctx):
        _clear_pending()
        decliner = make_ctx(supports_elicitation=True, elicit_response=None)
        _stage_ready_meeting(decliner)
        route = api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Hello there",
            ctx=decliner,
        )

        assert "the user declined" in result
        assert route.call_count == 0

    async def test_expired_pending_write_reports_expired_and_issues_no_post(
        self, api_mock, ctx, monkeypatch
    ):
        _clear_pending()
        times = iter([1000.0, 1000.0, 2000.0, 2000.0])
        monkeypatch.setattr(confirmations.time, "time", lambda: next(times))
        _stage_ready_meeting(ctx, ttl_seconds=5)
        route = api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Hello there",
            ctx=ctx,
        )

        assert "preview expired" in result
        assert route.call_count == 0

    async def test_attendee_disappearing_before_send_issues_no_post(
        self, api_mock, ctx
    ):
        _clear_pending()
        _stage_ready_meeting(ctx)
        api_mock.get("/users.php").mock(return_value=httpx.Response(404))
        route = api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(200, json={})
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Hello there",
            ctx=ctx,
        )

        assert "no longer exists" in result
        assert route.call_count == 0

    @pytest.mark.parametrize("status", [404, 400, 500])
    async def test_rejected_invite_returns_rejection_message_without_raising(
        self, api_mock, ctx, status
    ):
        _clear_pending()
        _stage_ready_meeting(ctx)
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json())
        )
        api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(status, text="nope")
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Hello there",
            ctx=ctx,
        )

        assert result == _rejection(status)

    @pytest.mark.parametrize(
        "response_json,expect_event,expect_join,expect_note",
        [
            (
                {"link": "https://cal/e1", "meeting_link": "https://meet/1"},
                True,
                True,
                False,
            ),
            ({"link": "https://cal/e1"}, True, False, False),
            ({"meeting_link": "https://meet/1"}, False, True, True),
            ({}, False, False, True),
        ],
    )
    async def test_success_response_renders_links_or_falls_back_to_a_note(
        self, api_mock, ctx, response_json, expect_event, expect_join, expect_note
    ):
        _clear_pending()
        _stage_ready_meeting(ctx)
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json())
        )
        api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(200, json=response_json)
        )

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description="Hello there",
            ctx=ctx,
        )

        assert "Calendar invite created and sent." in result
        assert ("Event:" in result) is expect_event
        assert ("Join:" in result) is expect_join
        assert ("Note:" in result) is expect_note


class TestPreviewThenScheduleIntegration:
    async def test_full_flow_posts_form_body_with_no_organizer_uid(self, api_mock, ctx):
        _clear_pending()
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_attendee_json())
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "events": [], "ooo": []}
            )
        )
        api_mock.get("/outlook-calendar").mock(return_value=httpx.Response(403))
        invite_route = api_mock.post("/google-calendar/schedule_meeting.php").mock(
            return_value=httpx.Response(
                200,
                json={"link": "https://cal/e1", "meeting_link": "https://meet/1"},
            )
        )
        outlook_invite = api_mock.post("/outlook-calendar/schedule_meeting.php")

        await preview_meeting(
            uid=42,
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description=None,
            ctx=ctx,
        )

        attendee = Attendee(
            name="Ada Lovelace",
            timezone="UTC",
            title="Engineer",
            city="London",
            state=None,
            username="ada",
        )
        expected_description = _default_description(attendee, 30, "Google Calendar")

        result = await schedule_meeting(
            attendee_name="Ada Lovelace",
            starts_at=FUTURE_ISO,
            duration_minutes=30,
            title="Sync up",
            description=expected_description,
            ctx=ctx,
        )

        assert invite_route.call_count == 1
        assert outlook_invite.call_count == 0

        sent = invite_route.calls.last.request
        body = urllib.parse.parse_qs(sent.content.decode())
        epoch, _, _ = _resolve_start_at(FUTURE_ISO)
        assert body["uid_to_meet"] == ["42"]
        assert body["starting_time"] == [str(epoch)]
        assert body["meeting_duration_min"] == ["30"]
        assert body["title"] == ["Sync up"]
        assert body["description"] == [expected_description]
        # Deliberate: the endpoint derives the organizer from the access token,
        # and sending this would be a client-supplied organizer.
        assert "organizer_uid" not in body

        assert "Calendar invite created and sent." in result
        assert "https://cal/e1" in result
        assert "https://meet/1" in result
