"""Tests for get_availability: working-hours math, calendar-provider probing,
busy-interval merging, and free-block computation.

Most of this module is pure logic, so the bulk of the coverage below calls the
private helpers directly rather than going through HTTP mocks — faster and it
pinpoints failures precisely. The end-to-end TestGetAvailability class at the
bottom exercises the full tool with respx-mocked /users.php and calendar
endpoints.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from goprofiles_mcp.tools import availability as av

UTC = dt.UTC


def _users_payload(uid=1, **overrides):
    """A users.php body with a full 7-day, all-day working-hours schedule.

    Covering every minute of every day (0-1439) means _todays_window always
    returns a window, regardless of what weekday the test actually runs on —
    the single biggest source of flakiness in this module's tests.
    """
    payload = {"uid": uid, "timezone": "America/New_York"}
    for key in av._DAY_KEYS:
        payload[f"working_hours_{key}_start"] = 0
        payload[f"working_hours_{key}_end"] = 1439
    payload.update(overrides)
    return payload


class _FrozenDatetime(dt.datetime):
    """Stand-in for the module's `datetime` name so `datetime.now()` is pinned.

    availability.py has no injectable clock, so the only way to make the full
    success path (current meeting, open blocks, time-off dates) fully
    deterministic is to replace the `datetime` symbol it imported.
    `fromtimestamp` is inherited unchanged, so _format_clock/_format_ooo still
    work normally against this fixed instant.
    """

    _fixed = dt.datetime(2026, 3, 10, 15, 30, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.astimezone(tz) if tz else cls._fixed


FIXED_NOW_UNIX = int(_FrozenDatetime._fixed.timestamp())


class TestAsEvent:
    def test_none_is_absent(self):
        assert av.as_event(None) is None

    def test_empty_list_is_absent(self):
        assert av.as_event([]) is None

    def test_empty_dict_is_absent(self):
        assert av.as_event({}) is None

    def test_zero_start_and_end_is_absent(self):
        assert av.as_event({"start_time": 0, "end_time": 0}) is None

    def test_real_dict_becomes_event(self):
        event = av.as_event({"start_time": 100, "end_time": 200, "title": "OOO"})
        assert event == av.CalendarEvent(title="OOO", start_time=100, end_time=200)

    def test_only_start_time_set_is_kept(self):
        event = av.as_event({"start_time": 100, "end_time": 0})
        assert event is not None
        assert event.start_time == 100


class TestParseWorkingHours:
    def test_parses_all_seven_days_and_timezone(self):
        hours = av._parse_working_hours(_users_payload())
        assert hours.timezone == "America/New_York"
        assert len(hours.days) == 7
        assert hours.days[0] == (0, 1439)

    def test_half_populated_day_is_skipped(self):
        payload = _users_payload(working_hours_tue_end=None)
        hours = av._parse_working_hours(payload)
        assert 2 not in hours.days

    def test_missing_day_key_is_skipped(self):
        payload = {"uid": 1}
        hours = av._parse_working_hours(payload)
        assert hours.days == {}

    def test_non_numeric_value_is_skipped(self):
        payload = _users_payload(working_hours_wed_start="not-a-number")
        hours = av._parse_working_hours(payload)
        assert 3 not in hours.days

    def test_blank_timezone_becomes_none(self):
        hours = av._parse_working_hours(_users_payload(timezone="   "))
        assert hours.timezone is None

    def test_non_string_timezone_becomes_none(self):
        hours = av._parse_working_hours(_users_payload(timezone=5))
        assert hours.timezone is None

    def test_missing_timezone_becomes_none(self):
        hours = av._parse_working_hours({"uid": 1})
        assert hours.timezone is None


class TestResolveZone:
    def test_valid_iana_name(self):
        zone = av._resolve_zone("America/New_York")
        assert zone is not None
        assert str(zone) == "America/New_York"

    def test_none_input(self):
        assert av._resolve_zone(None) is None

    def test_invalid_name_degrades_to_none(self):
        assert av._resolve_zone("Not/AZone") is None


class TestLocalDayIndex:
    def test_known_sunday(self):
        assert av._local_day_index(dt.datetime(2024, 1, 7, tzinfo=UTC)) == 0

    def test_known_monday(self):
        assert av._local_day_index(dt.datetime(2024, 1, 8, tzinfo=UTC)) == 1


class TestMinutesToUnix:
    def test_offset_math(self):
        day_start = dt.datetime(2024, 1, 8, 0, 0, 0, tzinfo=UTC)
        expected = int(day_start.timestamp()) + 90 * 60
        assert av._minutes_to_unix(day_start, 90) == expected


class TestTodaysWindow:
    def test_normal_entry(self):
        now_local = dt.datetime(2024, 1, 8, 10, 0, 0, tzinfo=UTC)  # Monday 10am
        hours = av.WorkingHours(days={1: (540, 1020)})
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        expected = (
            av._minutes_to_unix(midnight, 540),
            av._minutes_to_unix(midnight, 1020),
        )
        assert av._todays_window(now_local, hours) == expected

    def test_no_entry_for_today_is_none(self):
        now_local = dt.datetime(2024, 1, 8, 10, 0, 0, tzinfo=UTC)  # Monday
        hours = av.WorkingHours(days={})
        assert av._todays_window(now_local, hours) is None

    def test_overnight_shift_wraps_into_tomorrow(self):
        # Monday 11pm, entry runs 10pm-6am (start > end).
        now_local = dt.datetime(2024, 1, 8, 23, 0, 0, tzinfo=UTC)
        hours = av.WorkingHours(days={1: (1320, 360)})
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        expected = (
            av._minutes_to_unix(midnight, 1320),
            av._minutes_to_unix(midnight, 360 + av._MINUTES_PER_DAY),
        )
        assert av._todays_window(now_local, hours) == expected

    def test_inside_tail_of_yesterdays_overnight_shift(self):
        # Tuesday 2am, no entry for Tuesday itself; Monday's entry was an
        # overnight 10pm-6am shift whose tail (until 6am) still covers now.
        now_local = dt.datetime(2024, 1, 9, 2, 0, 0, tzinfo=UTC)
        hours = av.WorkingHours(days={1: (1320, 360)})
        prior_midnight = dt.datetime(2024, 1, 8, 0, 0, 0, tzinfo=UTC)
        expected = (
            av._minutes_to_unix(prior_midnight, 1320),
            av._minutes_to_unix(prior_midnight, 360 + av._MINUTES_PER_DAY),
        )
        assert av._todays_window(now_local, hours) == expected


class TestMergeBusy:
    def test_overlapping_intervals_merge(self):
        events = [
            av.CalendarEvent(start_time=100, end_time=200),
            av.CalendarEvent(start_time=150, end_time=300),
        ]
        assert av._merge_busy(events, (0, 1000)) == [(100, 300)]

    def test_touching_intervals_merge(self):
        events = [
            av.CalendarEvent(start_time=100, end_time=200),
            av.CalendarEvent(start_time=200, end_time=300),
        ]
        assert av._merge_busy(events, (0, 1000)) == [(100, 300)]

    def test_events_clipped_to_window_bounds(self):
        events = [av.CalendarEvent(start_time=50, end_time=150)]
        assert av._merge_busy(events, (100, 1000)) == [(100, 150)]

    def test_all_day_event_blankets_window_regardless_of_its_own_times(self):
        events = [av.CalendarEvent(start_time=5, end_time=5, is_all_day=True)]
        assert av._merge_busy(events, (1000, 2000)) == [(1000, 2000)]

    def test_unsorted_input_still_merges(self):
        events = [
            av.CalendarEvent(start_time=500, end_time=600),
            av.CalendarEvent(start_time=100, end_time=550),
        ]
        assert av._merge_busy(events, (0, 1000)) == [(100, 600)]

    def test_non_overlapping_stays_separate(self):
        events = [
            av.CalendarEvent(start_time=100, end_time=200),
            av.CalendarEvent(start_time=400, end_time=500),
        ]
        assert av._merge_busy(events, (0, 1000)) == [(100, 200), (400, 500)]


class TestOpenBlocks:
    # Gaps below need to clear _MIN_BLOCK_SECONDS (900s) to survive the floor,
    # so these use a much wider window than the merge/current-meeting tests.

    def test_gaps_between_busy_intervals(self):
        busy = [(1000, 2000), (4000, 5000)]
        blocks = av._open_blocks(0, (0, 10000), busy)
        assert blocks == [(0, 1000), (2000, 4000), (5000, 10000)]

    def test_cursor_starts_from_now_when_later_than_window_start(self):
        blocks = av._open_blocks(3000, (0, 10000), [])
        assert blocks == [(3000, 10000)]

    def test_short_block_is_dropped(self):
        busy = [(2000, 9200)]
        blocks = av._open_blocks(0, (0, 10000), busy)
        # The leading gap (0-2000) clears the 15-minute floor; the trailing
        # 800s tail after `busy` does not, so only the first block survives.
        assert blocks == [(0, 2000)]

    def test_no_gap_when_fully_busy(self):
        blocks = av._open_blocks(0, (0, 1000), [(0, 1000)])
        assert blocks == []


class TestCurrentMeeting:
    def test_event_containing_now_is_found(self):
        events = [av.CalendarEvent(start_time=100, end_time=200)]
        assert av._current_meeting(150, events) == events[0]

    def test_none_found_between_meetings(self):
        events = [
            av.CalendarEvent(start_time=100, end_time=200),
            av.CalendarEvent(start_time=300, end_time=400),
        ]
        assert av._current_meeting(250, events) is None


class TestFormatClock:
    def test_formats_utc_epoch(self):
        assert av._format_clock(0, UTC) == "12:00 AM"


class TestFormatMinutes:
    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (0, "12:00 AM"),
            (60, "1:00 AM"),
            (720, "12:00 PM"),
            (1439, "11:59 PM"),
            (1500, "1:00 AM"),  # wraps past midnight of the next day
        ],
    )
    def test_minute_offsets(self, minutes, expected):
        assert av._format_minutes(minutes) == expected


class TestFormatDaySpan:
    def test_contiguous_run_collapses(self):
        assert av._format_day_span([1, 2, 3, 4, 5]) == "Mon–Fri"

    def test_non_contiguous_stays_comma_separated(self):
        assert av._format_day_span([1, 3, 5]) == "Mon, Wed, Fri"

    def test_single_day(self):
        assert av._format_day_span([2]) == "Tue"

    def test_empty_list(self):
        assert av._format_day_span([]) == ""


class TestFormatSchedule:
    def test_no_days_is_unknown(self):
        assert av._format_schedule(av.WorkingHours(days={})) == "Unknown"

    def test_same_window_days_collapse_into_one_group(self):
        hours = av.WorkingHours(
            days={0: (540, 1020), 1: (540, 1020), 2: (540, 1020), 4: (540, 1020)}
        )
        assert av._format_schedule(hours) == "9:00 AM – 5:00 PM (Sun–Tue, Thu)"

    def test_distinct_windows_produce_separate_groups(self):
        hours = av.WorkingHours(days={0: (0, 60), 1: (120, 180)})
        assert (
            av._format_schedule(hours)
            == "12:00 AM – 1:00 AM (Sun); 2:00 AM – 3:00 AM (Mon)"
        )


class TestFormatMeetings:
    def test_past_only_meeting_is_filtered_out(self):
        event = av.CalendarEvent(start_time=100, end_time=200)
        assert av._format_meetings([event], 1000, UTC, "") == ["None"]

    def test_in_progress_meeting_gets_suffix(self):
        event = av.CalendarEvent(start_time=900, end_time=1100)
        lines = av._format_meetings([event], 1000, UTC, "")
        assert lines == ["12:15 AM – 12:18 AM  (in progress)"]

    def test_all_day_renders_as_all_day(self):
        event = av.CalendarEvent(start_time=0, end_time=2000, is_all_day=True)
        lines = av._format_meetings([event], 1000, UTC, "")
        assert lines == ["All day"]

    def test_no_upcoming_meetings_is_none(self):
        assert av._format_meetings([], 1000, UTC, "") == ["None"]

    def test_meetings_are_ordered_by_start_time(self):
        early = av.CalendarEvent(start_time=1200, end_time=1300)
        late = av.CalendarEvent(start_time=1400, end_time=1500)
        lines = av._format_meetings([late, early], 1000, UTC, "")
        assert lines == [
            "12:20 AM – 12:21 AM",
            "12:23 AM – 12:25 AM",
        ]


class TestFormatOoo:
    def test_none_is_not_scheduled(self):
        assert av._format_ooo(None, UTC) == "None scheduled"

    def test_single_day_range(self):
        event = av.CalendarEvent(start_time=0, end_time=3600)
        assert av._format_ooo(event, UTC) == "1970-01-01"

    def test_multi_day_range(self):
        event = av.CalendarEvent(start_time=0, end_time=200000)
        assert av._format_ooo(event, UTC) == "1970-01-01 to 1970-01-03"

    def test_none_zone_falls_back_to_utc(self):
        event = av.CalendarEvent(start_time=0, end_time=200000)
        assert av._format_ooo(event, None) == "1970-01-01 to 1970-01-03"


class TestPickReading:
    def test_ok_wins_over_everything(self):
        readings = [
            av.ProviderReading(outcome="error", provider="Outlook Calendar"),
            av.ProviderReading(outcome="ok", provider="Google Calendar"),
        ]
        assert av._pick_reading(readings).outcome == "ok"

    def test_not_connected_wins_over_error(self):
        readings = [
            av.ProviderReading(outcome="error", provider="Outlook Calendar"),
            av.ProviderReading(outcome="not_connected", provider="Google Calendar"),
        ]
        assert av._pick_reading(readings).outcome == "not_connected"

    def test_error_wins_over_not_enabled(self):
        readings = [
            av.ProviderReading(outcome="not_enabled", provider="Google Calendar"),
            av.ProviderReading(outcome="error", provider="Outlook Calendar"),
        ]
        assert av._pick_reading(readings).outcome == "error"

    def test_all_not_enabled_falls_back_to_default(self):
        readings = [
            av.ProviderReading(outcome="not_enabled", provider="Google Calendar"),
            av.ProviderReading(outcome="not_enabled", provider="Outlook Calendar"),
        ]
        reading = av._pick_reading(readings)
        assert reading.outcome == "not_enabled"
        assert reading.provider == ""


class TestGetAvailability:
    async def test_missing_ctx_raises(self):
        with pytest.raises(PermissionError):
            await av.get_availability(uid=1, ctx=None)

    async def test_uid_none_returns_guidance_and_makes_no_http_calls(
        self, ctx, api_mock
    ):
        result = await av.get_availability(uid=None, ctx=ctx)
        assert "requires a uid" in result
        assert not api_mock.calls

    async def test_users_404_returns_no_profile_found(self, ctx, api_mock):
        api_mock.get("/users.php").mock(return_value=httpx.Response(404))
        result = await av.get_availability(uid=1, ctx=ctx)
        assert "No profile found" in result

    async def test_users_body_without_uid_returns_no_profile_found(self, ctx, api_mock):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json={"name": "Nobody"})
        )
        result = await av.get_availability(uid=1, ctx=ctx)
        assert "No profile found" in result

    async def test_both_providers_disabled_reports_no_integration(self, ctx, api_mock):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload(timezone=None))
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(403, text="not enabled")
        )
        api_mock.get("/outlook-calendar").mock(
            return_value=httpx.Response(403, text="not enabled")
        )

        result = await av.get_availability(uid=1, ctx=ctx)

        assert "Availability (calendar unavailable):" in result
        assert "Status:         Unknown — no calendar data" in result
        assert "No calendar integration is connected" in result
        assert "Meetings left:" not in result
        assert "Next time off:" not in result

    async def test_one_provider_not_connected_reports_that_provider(
        self, ctx, api_mock
    ):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload(timezone=None))
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(200, json={"status": "failure"})
        )
        api_mock.get("/outlook-calendar").mock(
            return_value=httpx.Response(403, text="not enabled")
        )

        result = await av.get_availability(uid=1, ctx=ctx)

        assert "Availability (calendar unavailable):" in result
        assert "This person has not connected their Google Calendar" in result

    async def test_full_success_path(self, ctx, api_mock, monkeypatch):
        monkeypatch.setattr(av, "datetime", _FrozenDatetime)
        now = FIXED_NOW_UNIX

        current_meeting = av.CalendarEvent(
            title="busy", start_time=now - 1800, end_time=now + 1800
        )
        future_meeting = av.CalendarEvent(
            title="busy", start_time=now + 7200, end_time=now + 10800
        )
        ooo = {"start_time": now + 100000, "end_time": now + 300000}

        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload())
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "ok",
                    "events": [
                        future_meeting.model_dump(),
                        current_meeting.model_dump(),
                    ],
                    "ooo": ooo,
                },
            )
        )
        api_mock.get("/outlook-calendar").mock(
            return_value=httpx.Response(403, text="not enabled")
        )

        result = await av.get_availability(uid=1, ctx=ctx)

        assert "Availability (Google Calendar):" in result
        assert "Local time:     11:30 AM (America/New_York)" in result
        assert "Working hours:  12:00 AM – 11:59 PM (Sun–Sat)" in result
        assert "Status:         In a meeting until 12:00 PM" in result
        assert "Meetings left:  11:00 AM – 12:00 PM  (in progress)" in result
        assert "1:30 PM – 2:30 PM" in result
        assert "Open today:     12:00 PM – 1:30 PM, 2:30 PM – 11:59 PM" in result
        assert "Next time off:  2026-03-11 to 2026-03-13" in result

    async def test_no_timezone_reports_unknown_local_time_and_status(
        self, ctx, api_mock
    ):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload(timezone=None))
        )
        api_mock.get("/google-calendar").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "events": [], "ooo": None}
            )
        )
        api_mock.get("/outlook-calendar").mock(
            return_value=httpx.Response(403, text="not enabled")
        )

        result = await av.get_availability(uid=1, ctx=ctx)

        assert "Local time:     Unknown (no timezone set on their profile)" in result
        assert (
            "Status:         Free now — working hours unknown (no timezone set)"
            in result
        )
        assert (
            "Open today:     Unknown — no timezone set, so their day can't be "
            "placed on a clock" in result
        )
