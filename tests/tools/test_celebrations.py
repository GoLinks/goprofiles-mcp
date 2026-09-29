import httpx
import pytest

from goprofiles_mcp.tools.celebrations import (
    CelebrationResult,
    ResolvedFilters,
    _celebration_label,
    _format_celebration,
    _full_name,
    _when,
    _windows_summary,
    search_celebrations,
)


def _row(**overrides) -> dict:
    base = {
        "celebration": "birthday",
        "window": "upcoming",
        "celebration_date": "2026-08-20",
        "days_ago": None,
        "days_until": 3,
        "uid": 42,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "title": "Engineer",
        "department": "R&D",
        "hired_at": "2020-01-01",
        "birthday": "08-20",
        "years": None,
    }
    base.update(overrides)
    return base


def _celebrations_response(
    *rows, filters: dict | None = None, metadata: dict | None = None
):
    return {
        "metadata": {
            "limit": 50,
            "offset": 0,
            "total_results": len(rows),
            "count": len(rows),
            **(metadata or {}),
        },
        "filters": {
            "celebration_types": ["birthday", "anniversary", "new_hire"],
            "past_days": 21,
            "upcoming_days": 21,
            "new_hire_days": 35,
            **(filters or {}),
        },
        "results": list(rows),
    }


class TestFullName:
    def test_joins_first_and_last(self):
        c = CelebrationResult(first_name="Ada", last_name="Lovelace")
        assert _full_name(c) == "Ada Lovelace"

    def test_falls_back_to_username_when_names_blank(self):
        c = CelebrationResult(first_name="", last_name="", username="ada")
        assert _full_name(c) == "ada"

    def test_falls_back_to_unknown_when_nothing_present(self):
        c = CelebrationResult(first_name="", last_name="", username=None)
        assert _full_name(c) == "Unknown"


class TestCelebrationLabel:
    def test_birthday(self):
        c = CelebrationResult(celebration="birthday")
        assert _celebration_label(c) == "Birthday"

    def test_anniversary_with_years(self):
        c = CelebrationResult(celebration="anniversary", years=5)
        assert _celebration_label(c) == "5-year work anniversary"

    def test_anniversary_without_years(self):
        c = CelebrationResult(celebration="anniversary", years=None)
        assert _celebration_label(c) == "Work anniversary"

    def test_new_hire(self):
        c = CelebrationResult(celebration="new_hire")
        assert _celebration_label(c) == "New hire"

    def test_unknown_celebration_falls_back_to_raw_value(self):
        c = CelebrationResult(celebration="promotion")
        assert _celebration_label(c) == "promotion"

    def test_blank_celebration_reports_unknown(self):
        c = CelebrationResult(celebration="")
        assert _celebration_label(c) == "Unknown"


class TestWhen:
    def test_days_until_zero_is_today(self):
        c = CelebrationResult(days_until=0)
        assert _when(c) == "today"

    def test_days_until_one_is_singular(self):
        c = CelebrationResult(days_until=1)
        assert _when(c) == "in 1 day"

    def test_days_until_plural(self):
        c = CelebrationResult(days_until=3)
        assert _when(c) == "in 3 days"

    def test_days_ago_zero_is_today(self):
        c = CelebrationResult(days_ago=0)
        assert _when(c) == "today"

    def test_days_ago_one_is_singular(self):
        c = CelebrationResult(days_ago=1)
        assert _when(c) == "1 day ago"

    def test_days_ago_plural(self):
        c = CelebrationResult(days_ago=5)
        assert _when(c) == "5 days ago"

    def test_neither_present_reports_unknown(self):
        c = CelebrationResult(days_ago=None, days_until=None)
        assert _when(c) == "date unknown"

    def test_days_until_takes_priority_over_days_ago(self):
        c = CelebrationResult(days_until=0, days_ago=5)
        assert _when(c) == "today"


class TestFormatCelebration:
    def test_includes_title_department_and_started_when_present(self):
        c = CelebrationResult(**_row(celebration="anniversary", years=None))
        text = _format_celebration(c)
        assert "Title:       Engineer" in text
        assert "Department:  R&D" in text
        assert "Started:     2020-01-01" in text

    def test_omits_title_when_blank(self):
        c = CelebrationResult(**_row(title=None))
        assert "Title:" not in _format_celebration(c)

    def test_omits_department_when_blank(self):
        c = CelebrationResult(**_row(department=None))
        assert "Department:" not in _format_celebration(c)

    def test_omits_started_for_birthday_even_when_hired_at_present(self):
        c = CelebrationResult(**_row(celebration="birthday", hired_at="2020-01-01"))
        assert "Started:" not in _format_celebration(c)

    def test_omits_started_when_hired_at_blank(self):
        c = CelebrationResult(**_row(celebration="new_hire", hired_at=None))
        assert "Started:" not in _format_celebration(c)

    def test_includes_started_for_new_hire(self):
        c = CelebrationResult(**_row(celebration="new_hire", hired_at="2026-08-01"))
        assert "Started:     2026-08-01" in _format_celebration(c)

    def test_uid_line_marks_tool_use_only(self):
        c = CelebrationResult(**_row(uid=99))
        text = _format_celebration(c)
        assert "uid:         99  (tool use only — do not show to the user)" in text

    def test_username_falls_back_to_unknown(self):
        c = CelebrationResult(**_row(username=None))
        assert "Username:    Unknown" in _format_celebration(c)

    def test_date_falls_back_to_unknown(self):
        c = CelebrationResult(
            **_row(celebration_date=None, days_until=None, days_ago=None)
        )
        assert "Date:        Unknown (date unknown)" in _format_celebration(c)


class TestWindowsSummary:
    def test_basic_summary(self):
        f = ResolvedFilters(
            celebration_types=["birthday"],
            past_days=21,
            upcoming_days=21,
            new_hire_days=21,
        )
        assert _windows_summary(f) == "21 days back, 21 days ahead"

    def test_new_hire_days_differing_from_past_days_is_appended(self):
        f = ResolvedFilters(
            celebration_types=["new_hire"],
            past_days=21,
            upcoming_days=21,
            new_hire_days=35,
        )
        assert (
            _windows_summary(f)
            == "21 days back, 21 days ahead; new hires within 35 days"
        )

    def test_new_hire_days_equal_to_past_days_is_not_appended(self):
        f = ResolvedFilters(
            celebration_types=["new_hire"],
            past_days=35,
            upcoming_days=21,
            new_hire_days=35,
        )
        assert _windows_summary(f) == "35 days back, 21 days ahead"

    def test_new_hire_not_in_types_is_not_appended_even_if_days_differ(self):
        f = ResolvedFilters(
            celebration_types=["birthday"],
            past_days=21,
            upcoming_days=21,
            new_hire_days=35,
        )
        assert _windows_summary(f) == "21 days back, 21 days ahead"


class TestSearchCelebrations:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await search_celebrations(ctx=None)

    async def test_empty_results_reports_resolved_filter_windows(self, api_mock, ctx):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_celebrations_response(
                    filters={
                        "celebration_types": ["birthday"],
                        "past_days": 10,
                        "upcoming_days": 15,
                        "new_hire_days": 10,
                    }
                ),
            )
        )

        result = await search_celebrations(past_days=999, ctx=ctx)

        assert "No celebrations found for birthday" in result
        assert "10 days back, 15 days ahead" in result

    async def test_empty_results_default_types_label_when_none_resolved(
        self, api_mock, ctx
    ):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(
                200, json=_celebrations_response(filters={"celebration_types": []})
            )
        )

        result = await search_celebrations(ctx=ctx)

        assert "No celebrations found for birthday, anniversary, new_hire" in result

    async def test_splits_past_and_upcoming_into_labeled_sections_in_order(
        self, api_mock, ctx
    ):
        past_row = _row(
            celebration="birthday",
            window="past",
            days_ago=2,
            days_until=None,
            first_name="Grace",
            last_name="Hopper",
        )
        upcoming_row = _row(
            celebration="birthday",
            window="upcoming",
            days_ago=None,
            days_until=4,
            first_name="Ada",
            last_name="Lovelace",
        )
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(
                200, json=_celebrations_response(past_row, upcoming_row)
            )
        )

        result = await search_celebrations(ctx=ctx)

        recent_idx = result.index("Recent:")
        upcoming_idx = result.index("Upcoming:")
        assert recent_idx < upcoming_idx
        assert result.index("[1]") < result.index("Grace Hopper")
        assert result.index("[2]") < result.index("Ada Lovelace")
        assert result.index("Grace Hopper") < upcoming_idx

    async def test_leftover_window_value_still_appears(self, api_mock, ctx):
        odd_row = _row(window="someday", first_name="Mystery", last_name="Person")
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_celebrations_response(odd_row))
        )

        result = await search_celebrations(ctx=ctx)

        assert "Mystery Person" in result
        assert "[1]" in result

    async def test_celebration_types_joined_as_single_comma_separated_param(
        self, api_mock, ctx
    ):
        route = api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_celebrations_response())
        )

        await search_celebrations(celebration_types=["birthday", "new_hire"], ctx=ctx)

        sent = route.calls.last.request.url.params
        assert sent["celebration_types"] == "birthday,new_hire"
        assert sent.get_list("celebration_types") == ["birthday,new_hire"]

    async def test_past_days_zero_is_sent_not_omitted(self, api_mock, ctx):
        route = api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_celebrations_response())
        )

        await search_celebrations(past_days=0, ctx=ctx)

        assert route.calls.last.request.url.params["past_days"] == "0"

    async def test_upcoming_days_zero_is_sent_not_omitted(self, api_mock, ctx):
        route = api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_celebrations_response())
        )

        await search_celebrations(upcoming_days=0, ctx=ctx)

        assert route.calls.last.request.url.params["upcoming_days"] == "0"

    async def test_no_days_params_when_not_passed(self, api_mock, ctx):
        route = api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_celebrations_response())
        )

        await search_celebrations(ctx=ctx)

        params = route.calls.last.request.url.params
        assert "past_days" not in params
        assert "upcoming_days" not in params

    async def test_uid_appears_marked_tool_use_only(self, api_mock, ctx):
        row = _row(uid=777)
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_celebrations_response(row))
        )

        result = await search_celebrations(ctx=ctx)

        assert "uid:         777  (tool use only — do not show to the user)" in result

    async def test_http_error_propagates_as_runtime_error(self, api_mock, ctx):
        api_mock.get("/users.php").mock(return_value=httpx.Response(500, text="boom"))

        with pytest.raises(RuntimeError, match="status 500"):
            await search_celebrations(ctx=ctx)
