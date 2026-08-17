from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from goprofiles_mcp import confirmations
from goprofiles_mcp.tools.bravos import (
    _DEFAULT_TZ,
    BravoActivityResult,
    BravoTypeResult,
    _ago,
    _department_slug,
    _department_slugs,
    _filters_summary,
    _format_bravo_activity,
    _format_bravo_type,
    _match_score,
    _person,
    _points,
    _points_line,
    _relative,
    _resolve_send_at,
    _send_at_line,
    _sent_line,
    create_bravo,
    preview_bravo,
    search_bravo_types,
    search_bravos,
)


@pytest.fixture(autouse=True)
def _clear_pending():
    """Every create_bravo test drives the shared confirmations store, which is
    process-global — reset it around each test so tests can't see each other's
    staged writes."""
    with confirmations._lock:
        confirmations._pending.clear()
    yield
    with confirmations._lock:
        confirmations._pending.clear()


def _catalog_payload():
    return {
        "results": [
            {
                "bid": 1,
                "name": "Team Player",
                "description": "Recognizes great collaboration and teamwork",
            },
            {
                "bid": 2,
                "name": "Above And Beyond",
                "description": "Went the extra mile for the team",
            },
        ]
    }


@pytest.fixture
def catalog_route(api_mock):
    """GET /bravos.php returning a two-badge catalog — used by every tool that
    resolves a bid (search_bravo_types, preview_bravo, create_bravo)."""
    return api_mock.get("/bravos.php").mock(
        return_value=httpx.Response(200, json=_catalog_payload())
    )


@pytest.fixture
def user_route(api_mock):
    """GET /users.php resolving uid 42 to Jane Roe — the recipient every
    preview_bravo/create_bravo test targets unless it's specifically testing
    recipient-lookup failure."""
    return api_mock.get("/users.php").mock(
        return_value=httpx.Response(
            200,
            json={
                "uid": 42,
                "first_name": "Jane",
                "last_name": "Roe",
                "username": "jroe",
            },
        )
    )


def _activity_result(**overrides) -> dict:
    base = {
        "ubid": 1,
        "created_at": int(datetime.now(UTC).timestamp()) - 3600,
        "name": "Team Player",
        "points": None,
        "comment": None,
        "receiver_first_name": "Jane",
        "receiver_last_name": "Roe",
        "receiver_username": "jroe",
        "receiver_uid": 42,
        "receiver_department": "Engineering",
        "giver_first_name": "John",
        "giver_last_name": "Doe",
        "giver_username": "jdoe",
        "giver_uid": 7,
        "giver_department": "Sales",
    }
    base.update(overrides)
    return base


class TestMatchScore:
    def _badge(self, **overrides):
        base = {
            "bid": 1,
            "name": "Team Player",
            "description": "Recognizes great collaboration and teamwork",
        }
        base.update(overrides)
        return BravoTypeResult(**base)

    def test_empty_query_matches_everything_at_top_rank(self):
        assert _match_score(self._badge(), "   ") == 0

    def test_exact_name_match_is_best(self):
        assert _match_score(self._badge(), "team player") == 0

    def test_name_prefix_match(self):
        assert _match_score(self._badge(), "team") == 1

    def test_name_substring_match(self):
        assert _match_score(self._badge(), "player") == 2

    def test_description_substring_match(self):
        assert _match_score(self._badge(), "collaboration") == 3

    def test_no_match_returns_none(self):
        assert _match_score(self._badge(), "zzzznomatch") is None


class TestFormatBravoType:
    def test_formats_populated_badge(self):
        badge = BravoTypeResult(bid=7, name="Team Player", description="Great teamwork")
        text = _format_bravo_type(badge)
        assert "Name:        Team Player" in text
        assert "Description: Great teamwork" in text
        assert "bid:         7  (tool use only — do not show to the user)" in text

    def test_missing_name_and_description_use_defaults(self):
        badge = BravoTypeResult(bid=1, name="", description="")
        text = _format_bravo_type(badge)
        assert "Name:        Unknown" in text
        assert "Description: None" in text


class TestPoints:
    def test_none_normalizes_to_zero(self):
        assert _points(None) == 0

    def test_zero_stays_zero(self):
        assert _points(0) == 0

    def test_positive_value_is_preserved(self):
        assert _points(10) == 10


class TestPointsLine:
    def test_zero_reads_as_none(self):
        assert _points_line(0) == "none"

    def test_nonzero_is_stringified(self):
        assert _points_line(10) == "10"


class TestResolveSendAt:
    def test_none_means_send_immediately(self):
        assert _resolve_send_at(None) == (0, None)

    def test_blank_string_means_send_immediately(self):
        assert _resolve_send_at("   ") == (0, None)

    def test_unparseable_string_is_refused(self):
        epoch, error = _resolve_send_at("not-a-date")
        assert epoch == 0
        assert "not a valid date and time" in error
        assert "ISO 8601" in error

    def test_past_time_is_refused(self):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        epoch, error = _resolve_send_at(past)
        assert epoch == 0
        assert "is in the past" in error

    def test_more_than_a_year_ahead_is_refused(self):
        far_future = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        epoch, error = _resolve_send_at(far_future)
        assert epoch == 0
        assert "more than a year away" in error

    def test_naive_datetime_defaults_to_los_angeles(self):
        naive = "2027-01-15T09:00:00"
        expected = datetime(2027, 1, 15, 9, 0, 0, tzinfo=_DEFAULT_TZ)
        epoch, error = _resolve_send_at(naive)
        assert error is None
        assert epoch == int(expected.timestamp())

    def test_offset_aware_datetime_is_respected(self):
        future = datetime.now(UTC) + timedelta(days=10)
        value = future.isoformat()
        epoch, error = _resolve_send_at(value)
        assert error is None
        assert epoch == int(future.timestamp())


class TestRelative:
    def test_sub_minute_rounds_up_to_one_minute(self):
        # The displayed count is clamped to 1, but the plural suffix is chosen
        # from the raw (unclamped) minute count — 0 minutes reads as plural.
        assert _relative(10) == "about 1 minutes from now"

    def test_pluralizes_minutes(self):
        assert _relative(150) == "about 2 minutes from now"

    def test_singular_hour(self):
        assert _relative(3700) == "about 1 hour from now"

    def test_pluralizes_hours_under_two_days(self):
        assert _relative(90000) == "about 25 hours from now"

    def test_falls_back_to_days_at_or_past_48_hours(self):
        assert _relative(180000) == "about 2 days from now"


class TestAgo:
    def test_sub_minute_rounds_up_to_one_minute(self):
        assert _ago(10) == "1 minute ago"

    def test_pluralizes_minutes(self):
        assert _ago(150) == "2 minutes ago"

    def test_singular_hour(self):
        assert _ago(3700) == "1 hour ago"

    def test_pluralizes_hours_under_two_days(self):
        assert _ago(90000) == "25 hours ago"

    def test_falls_back_to_days_at_or_past_48_hours(self):
        assert _ago(180000) == "2 days ago"


class TestSendAtLine:
    def test_zero_epoch_means_immediately(self):
        assert _send_at_line(0) == "immediately"

    def test_nonzero_epoch_shows_absolute_and_relative(self):
        epoch = int((datetime.now(UTC) + timedelta(hours=1, minutes=1)).timestamp())
        line = _send_at_line(epoch)
        assert "—" in line
        assert "from now" in line
        expected_stamp = datetime.fromtimestamp(epoch, tz=_DEFAULT_TZ).strftime(
            "%a %d %b %Y, %H:%M %Z (UTC%z)"
        )
        assert expected_stamp in line


class TestSentLine:
    def test_none_is_unknown(self):
        assert _sent_line(None) == "Unknown"

    def test_past_timestamp_reads_ago(self):
        ts = int((datetime.now(UTC) - timedelta(hours=2)).timestamp())
        assert "ago" in _sent_line(ts)

    def test_future_scheduled_timestamp_reads_from_now(self):
        # A scheduled bravo is flagged sent while its time is still ahead; make
        # sure that doesn't render as '0 minutes ago'.
        ts = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
        assert "from now" in _sent_line(ts)


class TestPerson:
    def test_full_name_with_username_and_department(self):
        assert (
            _person("Jane", "Roe", "jroe", "Engineering")
            == "Jane Roe (jroe) — Engineering"
        )

    def test_full_name_without_username_or_department(self):
        assert _person("Jane", "Roe", None, None) == "Jane Roe"

    def test_full_name_without_department_omits_dash(self):
        assert _person("Jane", "Roe", "jroe", None) == "Jane Roe (jroe)"

    def test_blank_name_falls_back_to_username_without_repeating_it(self):
        assert _person("", "", "jdoe", None) == "jdoe"

    def test_blank_name_and_username_is_unknown(self):
        assert _person("", "", None, None) == "Unknown"


class TestFormatBravoActivity:
    def test_full_entry_includes_points_and_comment(self):
        b = BravoActivityResult.model_validate(
            _activity_result(points=10, comment="Great work!")
        )
        text = _format_bravo_activity(b)
        assert "Bravo:     Team Player" in text
        assert "From:      John Doe (jdoe) — Sales" in text
        assert "To:        Jane Roe (jroe) — Engineering" in text
        assert "Points:    10" in text
        assert "Comment:   Great work!" in text
        assert (
            "uid:       from 7 → to 42  (tool use only — do not show to the user)"
            in text
        )

    def test_no_points_or_comment_omits_those_lines(self):
        b = BravoActivityResult.model_validate(
            _activity_result(points=None, comment=None)
        )
        text = _format_bravo_activity(b)
        assert "Points:" not in text
        assert "Comment:" not in text


class TestDepartmentSlug:
    def test_simple_name(self):
        assert _department_slug("Customer Success") == "customer-success"

    def test_ampersand(self):
        assert _department_slug("R&D") == "r-d"

    def test_html_escaped_ampersand_unescapes_first(self):
        assert _department_slug("R&amp;D") == "r-d"

    def test_emoji_only_slugifies_away(self):
        assert _department_slug("😀😀") == ""

    def test_strips_surrounding_punctuation_and_whitespace(self):
        assert _department_slug("  Engineering!!  ") == "engineering"


class TestDepartmentSlugs:
    def test_none_returns_empty(self):
        assert _department_slugs(None) == ([], [])

    def test_dedupes_equivalent_slugs_and_collects_unusable(self):
        slugs, unusable = _department_slugs(["Engineering", "engineering", "R&D", "😀"])
        assert slugs == ["engineering", "r-d"]
        assert unusable == ["😀"]


class TestFiltersSummary:
    def test_no_filters(self):
        assert _filters_summary(None, None, None, None) == "all time; everyone"

    def test_days_singular(self):
        assert _filters_summary(1, None, None, None) == "last 1 day; everyone"

    def test_days_plural(self):
        assert _filters_summary(3, None, None, None) == "last 3 days; everyone"

    def test_person_and_departments_combine(self):
        summary = _filters_summary(7, "Jane", ["Engineering"], ["Sales", "CS"])
        assert summary == (
            "last 7 days; 'Jane' as giver or receiver; given by Engineering; "
            "received by Sales, CS"
        )


class TestSearchBravos:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await search_bravos(ctx=None)

    async def test_empty_results_with_no_filters(self, api_mock, ctx):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 0,
                        "count": 0,
                    },
                    "results": [],
                },
            )
        )
        result = await search_bravos(ctx=ctx)
        assert result == (
            "No bravos found for: all time; everyone. Tell the user nothing matched."
        )

    async def test_empty_results_with_days_filter_suggests_widening(
        self, api_mock, ctx
    ):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 0,
                        "count": 0,
                    },
                    "results": [],
                },
            )
        )
        result = await search_bravos(days=7, ctx=ctx)
        assert "widen the window with 'days'" in result

    async def test_empty_results_with_person_filter_suggests_last_name(
        self, api_mock, ctx
    ):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 0,
                        "count": 0,
                    },
                    "results": [],
                },
            )
        )
        result = await search_bravos(person_name="Zzzzz", ctx=ctx)
        assert "matched as a substring of 'first last'" in result

    async def test_empty_results_with_department_filter_suggests_checking_names(
        self, api_mock, ctx
    ):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 0,
                        "count": 0,
                    },
                    "results": [],
                },
            )
        )
        result = await search_bravos(giver_departments=["Engineering"], ctx=ctx)
        assert "department names match those in search_people" in result

    async def test_offset_past_total_results(self, api_mock, ctx):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 10,
                        "total_results": 5,
                        "count": 0,
                    },
                    "results": [],
                },
            )
        )
        result = await search_bravos(offset=10, ctx=ctx)
        assert result == (
            "No more bravos at offset 10 — this search has 5 result(s) in total. "
            "Lower 'offset' to page back through them."
        )

    async def test_department_filter_that_fully_slugifies_away_is_refused(
        self, api_mock, ctx
    ):
        route = api_mock.get("/activity.php").mock(return_value=httpx.Response(500))
        result = await search_bravos(giver_departments=["!!!", "###"], ctx=ctx)
        assert "none of the giver_departments names contain letters or digits" in result
        assert route.calls.call_count == 0

    async def test_successful_listing_formats_header_and_entries(self, api_mock, ctx):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 2,
                        "count": 2,
                    },
                    "results": [
                        _activity_result(),
                        _activity_result(ubid=2, name="Above And Beyond", giver_uid=8),
                    ],
                },
            )
        )
        result = await search_bravos(ctx=ctx)
        assert result.startswith(
            "Bravos (2 of 2 total, offset 0) — all time; everyone. Newest first:"
        )
        assert "[1]" in result
        assert "[2]" in result
        assert "Bravo:     Team Player" in result
        assert "Bravo:     Above And Beyond" in result

    async def test_entry_numbering_accounts_for_offset(self, api_mock, ctx):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 5,
                        "total_results": 6,
                        "count": 1,
                    },
                    "results": [_activity_result()],
                },
            )
        )
        result = await search_bravos(offset=5, ctx=ctx)
        assert "[6]" in result

    async def test_footer_names_dropped_department_names(self, api_mock, ctx):
        api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 1,
                        "count": 1,
                    },
                    "results": [_activity_result()],
                },
            )
        )
        result = await search_bravos(giver_departments=["Engineering", "!!!"], ctx=ctx)
        assert "ignored these department name(s)" in result
        assert "'!!!'" in result

    async def test_sends_department_filters_as_repeated_query_params(
        self, api_mock, ctx
    ):
        route = api_mock.get("/activity.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "metadata": {
                        "limit": 20,
                        "offset": 0,
                        "total_results": 1,
                        "count": 1,
                    },
                    "results": [_activity_result()],
                },
            )
        )
        await search_bravos(
            giver_departments=["Engineering", "Sales"],
            receiver_departments=["Customer Success"],
            ctx=ctx,
        )
        query = route.calls.last.request.url.params
        assert query.get_list("giver_departments[]") == ["engineering", "sales"]
        assert query.get_list("receiver_departments[]") == ["customer-success"]


class TestSearchBravoTypes:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await search_bravo_types(ctx=None)

    async def test_no_search_lists_everything(self, catalog_route, ctx):
        result = await search_bravo_types(ctx=ctx)
        assert "Bravo badge types (all 2 available in this workspace):" in result
        assert "Team Player" in result
        assert "Above And Beyond" in result
        assert "bid:         1  (tool use only" in result
        assert "bid:         2  (tool use only" in result

    async def test_search_filters_by_name_substring(self, catalog_route, ctx):
        result = await search_bravo_types(search="beyond", ctx=ctx)
        assert "1 of 1 matched 'beyond'" in result
        assert "Above And Beyond" in result
        assert "Team Player" not in result

    async def test_search_filters_by_description_substring(self, catalog_route, ctx):
        result = await search_bravo_types(search="extra mile", ctx=ctx)
        assert "Above And Beyond" in result
        assert "Team Player" not in result

    async def test_search_with_no_matches(self, catalog_route, ctx):
        result = await search_bravo_types(search="zzzznomatch", ctx=ctx)
        assert "No bravo badge types matched that search" in result

    async def test_empty_catalog_reports_no_badges_available(self, api_mock, ctx):
        api_mock.get("/bravos.php").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await search_bravo_types(ctx=ctx)
        assert "no giveable bravo badge types" in result


class TestPreviewBravo:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await preview_bravo(receiver_uid=1, bid=1, comment="hi", ctx=None)

    async def test_empty_comment_is_refused(self, ctx):
        result = await preview_bravo(receiver_uid=42, bid=1, comment="   ", ctx=ctx)
        assert "No preview — the message is empty" in result
        assert confirmations._pending == {}

    async def test_unknown_bid_is_refused(self, catalog_route, ctx):
        result = await preview_bravo(
            receiver_uid=42, bid=999, comment="Great job!", ctx=ctx
        )
        assert "not in this workspace's catalog" in result
        assert confirmations._pending == {}

    async def test_unknown_receiver_uid_is_refused(self, catalog_route, api_mock, ctx):
        api_mock.get("/users.php").mock(return_value=httpx.Response(404))
        result = await preview_bravo(
            receiver_uid=999, bid=1, comment="Great job!", ctx=ctx
        )
        assert "No preview — no person found with that uid" in result
        assert confirmations._pending == {}

    async def test_unparseable_send_at_is_refused_without_staging(
        self, catalog_route, user_route, ctx
    ):
        result = await preview_bravo(
            receiver_uid=42, bid=1, comment="Great job!", send_at="not-a-date", ctx=ctx
        )
        assert "No preview — 'not-a-date' is not a valid date and time" in result
        assert confirmations._pending == {}

    async def test_past_send_at_is_refused_without_staging(
        self, catalog_route, user_route, ctx
    ):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        result = await preview_bravo(
            receiver_uid=42, bid=1, comment="Great job!", send_at=past, ctx=ctx
        )
        assert "No preview — " in result
        assert "is in the past" in result
        assert confirmations._pending == {}

    async def test_send_at_more_than_a_year_out_is_refused_without_staging(
        self, catalog_route, user_route, ctx
    ):
        far_future = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        result = await preview_bravo(
            receiver_uid=42, bid=1, comment="Great job!", send_at=far_future, ctx=ctx
        )
        assert "more than a year away" in result
        assert confirmations._pending == {}

    async def test_successful_preview_stages_and_returns_preview_text(
        self, catalog_route, user_route, ctx
    ):
        result = await preview_bravo(
            receiver_uid=42, bid=1, comment="Great job!", ctx=ctx
        )
        assert "Bravo previewed — NOT sent." in result
        assert "To:      Jane Roe" in result
        assert "Badge:   Team Player" in result
        assert "Points:  none" in result
        assert "Sends:   immediately" in result
        assert "Message:\nGreat job!" in result
        assert confirmations._pending != {}

    async def test_preview_with_points_and_send_at_shows_caveats(
        self, catalog_route, user_route, ctx
    ):
        send_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        result = await preview_bravo(
            receiver_uid=42,
            bid=1,
            comment="Great job!",
            points=10,
            send_at=send_at,
            ctx=ctx,
        )
        assert "Points:  10" in result
        assert "cannot be cancelled or rescheduled from this chat" in result
        assert "10 point(s) leave their balance" in result
        assert "Delivered on the next hourly run at or after that time." in result

    async def test_preview_then_create_proceeds_with_matching_confirm_args(
        self, catalog_route, user_route, api_mock, ctx
    ):
        # Confirms preview_bravo staged the payload/confirm_args create_bravo
        # expects, by driving the whole handshake end to end.
        post_route = api_mock.post("/bravos.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "message": "",
                    "successful_count": 1,
                    "failed_count": 0,
                    "total_count": 1,
                    "comment_id": 1,
                    "scheduled": False,
                },
            )
        )
        await preview_bravo(receiver_uid=42, bid=1, comment="Great job!", ctx=ctx)
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=ctx,
        )
        assert "Bravo sent successfully" in result
        assert post_route.calls.call_count == 1


class TestCreateBravo:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await create_bravo(
                recipient_name="Jane Roe",
                badge_name="Team Player",
                comment="hi",
                ctx=None,
            )

    async def test_nothing_pending_makes_no_post(self, api_mock, ctx):
        post_route = api_mock.post("/bravos.php").mock(return_value=httpx.Response(200))
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=ctx,
        )
        assert "there is no Bravo waiting to be sent" in result
        assert post_route.calls.call_count == 0

    async def test_declined_makes_no_post(
        self, catalog_route, user_route, api_mock, make_ctx
    ):
        post_route = api_mock.post("/bravos.php").mock(return_value=httpx.Response(200))
        preview_ctx = make_ctx()
        await preview_bravo(
            receiver_uid=42, bid=1, comment="Great job!", ctx=preview_ctx
        )

        decline_ctx = make_ctx(supports_elicitation=True, elicit_response=None)
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=decline_ctx,
        )
        assert "No Bravo sent — the user declined" in result
        assert post_route.calls.call_count == 0

    async def test_drifted_makes_no_post(
        self, catalog_route, user_route, api_mock, ctx
    ):
        post_route = api_mock.post("/bravos.php").mock(return_value=httpx.Response(200))
        await preview_bravo(receiver_uid=42, bid=1, comment="Great job!", ctx=ctx)

        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="A different message entirely",
            ctx=ctx,
        )
        assert "do not match the preview" in result
        assert post_route.calls.call_count == 0

    async def test_expired_makes_no_post(
        self, catalog_route, user_route, api_mock, ctx
    ):
        post_route = api_mock.post("/bravos.php").mock(return_value=httpx.Response(200))
        await preview_bravo(receiver_uid=42, bid=1, comment="Great job!", ctx=ctx)

        # Monkeypatching time.time globally would also affect httpx/respx's own
        # use of it mid-request, so backdate the staged entry directly instead
        # — the same private store test_confirmations.py reaches into.
        key = confirmations._owner_key(ctx, "create_bravo")
        with confirmations._lock:
            confirmations._pending[key].expires_at = 0

        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=ctx,
        )
        assert "No Bravo sent — the preview expired" in result
        assert post_route.calls.call_count == 0

    async def test_full_successful_flow_posts_form_encoded_data(
        self, catalog_route, user_route, api_mock, ctx
    ):
        post_route = api_mock.post("/bravos.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "message": "",
                    "successful_count": 1,
                    "failed_count": 0,
                    "total_count": 1,
                    "comment_id": 1,
                    "scheduled": False,
                },
            )
        )
        await preview_bravo(receiver_uid=42, bid=1, comment="Great job!", ctx=ctx)
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=ctx,
        )

        assert post_route.calls.call_count == 1
        sent = post_route.calls.last.request.read().decode()
        params = httpx.QueryParams(sent)
        assert params["bid"] == "1"
        assert params["comment"] == "Great job!"
        assert params["receiver_uids[]"] == "42"
        assert "points" not in params
        assert "scheduled_time" not in params
        assert "Bravo sent successfully" in result
        assert "Badge:      Team Player" in result
        assert "To:         Jane Roe" in result

    async def test_points_and_send_at_are_included_when_set(
        self, catalog_route, user_route, api_mock, ctx
    ):
        send_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        post_route = api_mock.post("/bravos.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "message": "",
                    "successful_count": 1,
                    "failed_count": 0,
                    "total_count": 1,
                    "comment_id": 1,
                    "scheduled": True,
                },
            )
        )
        await preview_bravo(
            receiver_uid=42,
            bid=1,
            comment="Great job!",
            points=10,
            send_at=send_at,
            ctx=ctx,
        )
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            points=10,
            send_at=send_at,
            ctx=ctx,
        )

        sent = post_route.calls.last.request.read().decode()
        params = httpx.QueryParams(sent)
        assert params["points"] == "10"
        assert int(params["scheduled_time"]) > 0
        assert "Points:     10" in result
        assert "Bravo scheduled" in result

    @pytest.mark.parametrize("status_code", [400, 422])
    async def test_rejection_after_points_and_send_at_guesses_reason(
        self, catalog_route, user_route, api_mock, ctx, status_code
    ):
        send_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        api_mock.post("/bravos.php").mock(
            return_value=httpx.Response(status_code, text="rejected")
        )
        await preview_bravo(
            receiver_uid=42,
            bid=1,
            comment="Great job!",
            points=500,
            send_at=send_at,
            ctx=ctx,
        )
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            points=500,
            send_at=send_at,
            ctx=ctx,
        )
        assert "No Bravo sent — GoProfiles rejected it" in result
        assert "500 points may exceed" in result
        assert "the scheduled time may no longer be in the future" in result

    async def test_recipient_disappearing_before_send_is_refused(
        self, catalog_route, api_mock, ctx
    ):
        api_mock.get("/users.php").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "uid": 42,
                        "first_name": "Jane",
                        "last_name": "Roe",
                        "username": "jroe",
                    },
                ),
                httpx.Response(404),
            ]
        )
        post_route = api_mock.post("/bravos.php").mock(return_value=httpx.Response(200))

        await preview_bravo(receiver_uid=42, bid=1, comment="Great job!", ctx=ctx)
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=ctx,
        )

        assert "that person no longer exists in GoProfiles" in result
        assert post_route.calls.call_count == 0

    async def test_api_reported_failure_is_surfaced(
        self, catalog_route, user_route, api_mock, ctx
    ):
        api_mock.post("/bravos.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "failed",
                    "message": "Recipient has opted out.",
                    "successful_count": 0,
                    "failed_count": 1,
                    "total_count": 1,
                    "comment_id": None,
                    "scheduled": False,
                },
            )
        )
        await preview_bravo(receiver_uid=42, bid=1, comment="Great job!", ctx=ctx)
        result = await create_bravo(
            recipient_name="Jane Roe",
            badge_name="Team Player",
            comment="Great job!",
            ctx=ctx,
        )
        assert "Failed to send Bravo. Recipient has opted out." in result
