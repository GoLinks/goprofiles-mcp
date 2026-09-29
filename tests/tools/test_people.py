import json

import httpx
import pytest

from goprofiles_mcp.tools.people import (
    PersonResult,
    Profile,
    ProfileCertification,
    ProfileContact,
    ProfileLanguage,
    _allow_fields,
    _format_list,
    _format_location,
    _format_person,
    _format_profile,
    _full_name,
    _match_quality,
    _names_from_items,
    _normalize_profile,
    _optional_str,
    get_profile,
    search_people,
)


def _users_payload(**overrides) -> dict:
    payload = {
        "uid": 42,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "title": "Engineer",
        "department": "R&D",
        "city": "London",
        "state": "",
        "country": "UK",
        "skills": [{"name": "Math"}],
        "interests": [{"name": "Poetry"}],
        "groups": [{"name": "Founders"}],
    }
    payload.update(overrides)
    return payload


class TestMatchQuality:
    def test_below_exact_threshold_is_exact_name_match(self):
        assert _match_quality(1.0) == "exact name match"

    def test_at_exact_threshold_boundary_is_partial(self):
        assert _match_quality(1.16) == "partial name match"

    def test_between_thresholds_is_partial_name_match(self):
        assert _match_quality(1.9) == "partial name match"

    def test_at_partial_threshold_boundary_is_non_name(self):
        assert _match_quality(2.0) == "matched on title/department/skill/etc., not name"

    def test_above_partial_threshold_is_non_name(self):
        assert _match_quality(5.0) == "matched on title/department/skill/etc., not name"


class TestFullName:
    def test_combines_first_and_last(self):
        p = PersonResult(first_name="Ada", last_name="Lovelace")
        assert _full_name(p) == "Ada Lovelace"

    def test_falls_back_to_username_when_names_blank(self):
        p = PersonResult(first_name="", last_name="", username="ada")
        assert _full_name(p) == "ada"

    def test_falls_back_to_unknown_when_all_blank(self):
        p = PersonResult(first_name="", last_name="", username="")
        assert _full_name(p) == "Unknown"


class TestFormatPerson:
    def _person(self, **overrides) -> PersonResult:
        defaults = {
            "uid": 7,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "username": "ada",
            "title": "Engineer",
            "department_name": "R&D",
            "location": "London, UK",
            "user_skills": "Python, Math",
            "user_interests": "Poetry",
            "user_languages": "English",
            "user_groups": "Founders",
            "reports": 0,
            "unlicensed_profile": None,
            "rating": 0.0,
        }
        defaults.update(overrides)
        return PersonResult(**defaults)

    def test_always_includes_core_fields_and_uid_warning(self):
        text = _format_person(self._person(), show=set(), show_match=False)
        assert "Name:       Ada Lovelace" in text
        assert "uid:        7  (tool use only — do not show to the user)" in text

    def test_facet_line_hidden_when_key_not_in_show(self):
        text = _format_person(self._person(), show=set(), show_match=False)
        assert "Skills:" not in text

    def test_facet_line_hidden_when_value_falsy_even_if_shown(self):
        p = self._person(user_skills=None)
        text = _format_person(p, show={"skills"}, show_match=False)
        assert "Skills:" not in text

    def test_facet_line_shown_when_key_in_show_and_value_truthy(self):
        text = _format_person(self._person(), show={"skills"}, show_match=False)
        assert "Skills:     Python, Math" in text

    def test_multiple_facets_shown_independently(self):
        text = _format_person(
            self._person(), show={"skills", "groups"}, show_match=False
        )
        assert "Skills:" in text
        assert "Groups:" in text
        assert "Interests:" not in text
        assert "Languages:" not in text

    def test_reports_line_shown_only_when_nonzero(self):
        text = _format_person(self._person(reports=3), show=set(), show_match=False)
        assert "Reports:    3 direct report(s)" in text
        text_none = _format_person(
            self._person(reports=0), show=set(), show_match=False
        )
        assert "Reports:" not in text_none

    def test_unlicensed_note_shown_only_when_truthy(self):
        text = _format_person(
            self._person(unlicensed_profile=1), show=set(), show_match=False
        )
        assert "Note:       Unlicensed profile — may have incomplete data." in text
        text_none = _format_person(
            self._person(unlicensed_profile=None), show=set(), show_match=False
        )
        assert "Note:" not in text_none

    def test_match_line_only_shown_when_show_match_true(self):
        text = _format_person(self._person(rating=0.5), show=set(), show_match=True)
        assert "Match:      exact name match" in text
        text_hidden = _format_person(
            self._person(rating=0.5), show=set(), show_match=False
        )
        assert "Match:" not in text_hidden


class TestAllowFields:
    def test_copies_only_allowed_keys(self):
        raw = {"a": 1, "b": 2, "c": 3}
        assert _allow_fields(raw, frozenset({"a", "c"})) == {"a": 1, "c": 3}

    def test_missing_allowed_keys_are_skipped(self):
        raw = {"a": 1}
        assert _allow_fields(raw, frozenset({"a", "b"})) == {"a": 1}


class TestNamesFromItems:
    def test_skips_non_dict_items(self):
        assert _names_from_items([{"name": "Python"}, "oops", 5, None]) == ["Python"]

    def test_strips_whitespace(self):
        assert _names_from_items([{"name": "  Python  "}]) == ["Python"]

    def test_drops_blank_names(self):
        assert _names_from_items([{"name": "   "}, {"name": ""}]) == []

    def test_non_list_input_returns_empty(self):
        assert _names_from_items("not a list") == []
        assert _names_from_items(None) == []


class TestFormatLocation:
    def test_joins_all_present_parts(self):
        assert _format_location("London", "England", "UK") == "London, England, UK"

    def test_drops_blank_and_non_string_parts(self):
        assert _format_location("London", "", None) == "London"
        assert _format_location("London", 5, "UK") == "London, UK"

    def test_returns_none_when_all_parts_missing(self):
        assert _format_location(None, "", None) is None


class TestOptionalStr:
    def test_strips_and_returns_string(self):
        assert _optional_str("  hi  ") == "hi"

    def test_blank_string_returns_none(self):
        assert _optional_str("   ") is None

    def test_non_string_returns_none(self):
        assert _optional_str(5) is None
        assert _optional_str(None) is None


class TestNormalizeProfile:
    def test_combines_all_three_sources(self):
        profile = _normalize_profile(
            _users_payload(),
            [{"name": "AWS Cert", "category": "Cloud"}],
            [{"name": "English", "code": "en"}],
        )
        assert profile.first_name == "Ada"
        assert profile.skills == ["Math"]
        assert profile.certifications[0].name == "AWS Cert"
        assert profile.languages[0].code == "en"
        assert profile.location == "London, UK"

    def test_drops_keys_outside_allow_list(self):
        profile = _normalize_profile(
            _users_payload(internal_notes="do not leak this"),
            [],
            [],
        )
        assert "internal_notes" not in profile.model_dump()
        assert "do not leak this" not in _format_profile(profile)

    def test_certification_and_language_items_use_their_own_allow_lists(self):
        profile = _normalize_profile(
            _users_payload(),
            [{"name": "AWS", "internal_id": "secret"}],
            [{"name": "French", "code": "fr", "internal_id": "secret"}],
        )
        assert "internal_id" not in profile.certifications[0].model_dump()
        assert "internal_id" not in profile.languages[0].model_dump()

    def test_non_dict_items_in_lists_are_ignored(self):
        profile = _normalize_profile(_users_payload(), ["oops", {"name": "AWS"}], [])
        assert len(profile.certifications) == 1


class TestFormatList:
    def test_joins_values(self):
        assert _format_list(["a", "b"]) == "a, b"

    def test_empty_list_is_none(self):
        assert _format_list([]) == "None"


class TestFormatProfile:
    def _profile(self, **overrides) -> Profile:
        defaults = {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "username": "ada",
            "title": None,
            "department": None,
            "bio": None,
            "pronouns": None,
            "location": None,
            "timezone": None,
            "contact": ProfileContact(),
            "skills": [],
            "interests": [],
            "groups": [],
            "languages": [],
            "certifications": [],
            "profile_link": None,
            "unlicensed_profile": None,
        }
        defaults.update(overrides)
        return Profile(**defaults)

    def test_unknown_fallbacks_for_missing_scalars(self):
        text = _format_profile(self._profile())
        assert "Title:         Unknown" in text
        assert "Department:    Unknown" in text
        assert "Location:      Unknown" in text
        assert "Timezone:      Unknown" in text
        assert "Pronouns:      Unknown" in text
        assert "Bio:           None" in text
        assert "Profile link:  Unknown" in text

    def test_contact_unknown_and_none_fallbacks(self):
        text = _format_profile(self._profile())
        assert "Email:             Unknown" in text
        assert "Phone extension:   None" in text

    def test_lists_fall_back_to_none_when_empty(self):
        text = _format_profile(self._profile())
        assert "Skills:        None" in text
        assert "Interests:     None" in text
        assert "Groups:        None" in text
        assert "Languages:     None" in text
        assert "Certifications: None" in text

    def test_certifications_render_multiline_with_all_extras(self):
        cert = ProfileCertification(
            name="AWS Cert",
            category="Cloud",
            issue_date="2020-01-01",
            expiration_date="2023-01-01",
            credential_id="ABC123",
        )
        text = _format_profile(self._profile(certifications=[cert]))
        assert (
            "  - AWS Cert (Cloud; issued 2020-01-01; expires 2023-01-01; id ABC123)"
            in text
        )

    def test_certification_with_no_extras_shows_bare_name(self):
        cert = ProfileCertification(name="AWS Cert")
        text = _format_profile(self._profile(certifications=[cert]))
        assert "  - AWS Cert" in text
        assert "AWS Cert (" not in text

    def test_certification_missing_name_falls_back_to_unknown(self):
        cert = ProfileCertification(name="", category="Cloud")
        text = _format_profile(self._profile(certifications=[cert]))
        assert "  - Unknown (Cloud)" in text

    def test_language_with_code_shown_in_parens(self):
        lang = ProfileLanguage(name="English", code="en")
        text = _format_profile(self._profile(languages=[lang]))
        assert "Languages:     English (en)" in text

    def test_language_without_code_shown_bare(self):
        lang = ProfileLanguage(name="English", code="")
        text = _format_profile(self._profile(languages=[lang]))
        assert "Languages:     English" in text
        assert "English (" not in text

    def test_unlicensed_note_shown_only_when_truthy(self):
        text = _format_profile(self._profile(unlicensed_profile=1))
        assert "Note:          Unlicensed profile — may have incomplete data." in text
        text_none = _format_profile(self._profile(unlicensed_profile=None))
        assert "Note:" not in text_none

    def test_name_falls_back_to_username_then_unknown(self):
        text = _format_profile(
            self._profile(first_name="", last_name="", username="ada")
        )
        assert "Name:          ada" in text
        text2 = _format_profile(self._profile(first_name="", last_name="", username=""))
        assert "Name:          Unknown" in text2


def _people_response(**overrides) -> dict:
    body = {
        "metadata": {"limit": 20, "offset": 0, "total_results": 1, "count": 1},
        "results": [
            {
                "uid": 1,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "username": "ada",
                "title": "Engineer",
                "department_name": "R&D",
                "location": "London, UK",
                "rating": 1.0,
            }
        ],
    }
    body.update(overrides)
    return body


class TestSearchPeoplePermission:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError):
            await search_people(ctx=None)


class TestSearchPeopleParams:
    async def test_names_become_json_encoded_search_terms(self, ctx, api_mock):
        route = api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        await search_people(names=["ada", "lovelace"], ctx=ctx)
        sent = route.calls.last.request.url.params
        assert sent["search_terms"] == json.dumps(["ada", "lovelace"])

    async def test_omitting_names_sends_empty_search_terms(self, ctx, api_mock):
        route = api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        await search_people(ctx=ctx)
        sent = route.calls.last.request.url.params
        assert sent["search_terms"] == "[]"

    async def test_json_filters_only_sent_when_non_empty(self, ctx, api_mock):
        route = api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        await search_people(skills=["Python"], departments=["Engineering"], ctx=ctx)
        sent = route.calls.last.request.url.params
        assert sent["skills"] == json.dumps(["Python"])
        assert sent["departments"] == json.dumps(["Engineering"])
        assert "titles" not in sent
        assert "locations" not in sent

    async def test_empty_filter_lists_are_omitted(self, ctx, api_mock):
        route = api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        await search_people(skills=[], ctx=ctx)
        sent = route.calls.last.request.url.params
        assert "skills" not in sent

    async def test_sort_and_order_passed_through_when_set(self, ctx, api_mock):
        route = api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        await search_people(sort="hired_at", order="asc", ctx=ctx)
        sent = route.calls.last.request.url.params
        assert sent["sort"] == "hired_at"
        assert sent["order"] == "asc"

    async def test_sort_and_order_omitted_when_not_set(self, ctx, api_mock):
        route = api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        await search_people(ctx=ctx)
        sent = route.calls.last.request.url.params
        assert "sort" not in sent
        assert "order" not in sent


class TestSearchPeopleNoResults:
    async def test_no_results_with_names_mentions_substring_tip(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
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
        result = await search_people(names=["nobody"], ctx=ctx)
        assert "substring-based" in result

    async def test_no_results_without_names_is_plain_message(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
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
        result = await search_people(departments=["Nowhere"], ctx=ctx)
        assert result == "No people found."


class TestSearchPeopleResults:
    async def test_header_shows_count_total_and_offset(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_people_response(
                    metadata={
                        "limit": 20,
                        "offset": 5,
                        "total_results": 12,
                        "count": 1,
                    }
                ),
            )
        )
        result = await search_people(departments=["R&D"], ctx=ctx)
        assert "People (1 of 12 total, offset 5):" in result

    async def test_facet_column_only_rendered_when_filter_passed(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_people_response(
                    results=[
                        {
                            "uid": 1,
                            "first_name": "Ada",
                            "last_name": "Lovelace",
                            "username": "ada",
                            "user_skills": "Python",
                        }
                    ]
                ),
            )
        )
        result = await search_people(skills=["Python"], ctx=ctx)
        assert "Skills:     Python" in result

        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_people_response(
                    results=[
                        {
                            "uid": 1,
                            "first_name": "Ada",
                            "last_name": "Lovelace",
                            "username": "ada",
                            "user_skills": "Python",
                        }
                    ]
                ),
            )
        )
        result_no_filter = await search_people(departments=["R&D"], ctx=ctx)
        assert "Skills:" not in result_no_filter

    async def test_match_line_only_when_names_passed(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        result_names = await search_people(names=["ada"], ctx=ctx)
        assert "Match:" in result_names

        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        result_no_names = await search_people(departments=["R&D"], ctx=ctx)
        assert "Match:" not in result_no_names

    async def test_multiple_results_with_names_appends_footer(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_people_response(
                    metadata={
                        "limit": 20,
                        "offset": 0,
                        "total_results": 2,
                        "count": 2,
                    },
                    results=[
                        {
                            "uid": 1,
                            "first_name": "Ada",
                            "last_name": "Lovelace",
                            "username": "ada",
                        },
                        {
                            "uid": 2,
                            "first_name": "Grace",
                            "last_name": "Hopper",
                            "username": "grace",
                        },
                    ],
                ),
            )
        )
        result = await search_people(names=["a"], ctx=ctx)
        assert "Multiple people matched" in result

    async def test_single_result_with_names_has_no_footer(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(200, json=_people_response())
        )
        result = await search_people(names=["ada"], ctx=ctx)
        assert "Multiple people matched" not in result

    async def test_filter_only_multi_result_has_no_footer(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_people_response(
                    metadata={
                        "limit": 20,
                        "offset": 0,
                        "total_results": 2,
                        "count": 2,
                    },
                    results=[
                        {
                            "uid": 1,
                            "first_name": "Ada",
                            "last_name": "Lovelace",
                            "username": "ada",
                        },
                        {
                            "uid": 2,
                            "first_name": "Grace",
                            "last_name": "Hopper",
                            "username": "grace",
                        },
                    ],
                ),
            )
        )
        result = await search_people(departments=["R&D"], ctx=ctx)
        assert "Multiple people matched" not in result

    async def test_uid_marked_tool_use_only_in_every_entry(self, ctx, api_mock):
        api_mock.get("/search/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_people_response(
                    metadata={
                        "limit": 20,
                        "offset": 0,
                        "total_results": 2,
                        "count": 2,
                    },
                    results=[
                        {
                            "uid": 1,
                            "first_name": "Ada",
                            "last_name": "Lovelace",
                            "username": "ada",
                        },
                        {
                            "uid": 2,
                            "first_name": "Grace",
                            "last_name": "Hopper",
                            "username": "grace",
                        },
                    ],
                ),
            )
        )
        result = await search_people(departments=["R&D"], ctx=ctx)
        assert result.count("(tool use only — do not show to the user)") == 2


class TestGetProfilePermission:
    async def test_missing_ctx_raises_permission_error(self):
        with pytest.raises(PermissionError):
            await get_profile(uid=1, ctx=None)


class TestGetProfileNoUid:
    async def test_none_uid_returns_guidance_with_no_http_calls(self, ctx, api_mock):
        result = await get_profile(uid=None, ctx=ctx)
        assert "requires a uid" in result
        assert api_mock.calls.call_count == 0


class TestGetProfileNotFound:
    async def test_users_404_returns_no_profile_found(self, ctx, api_mock):
        api_mock.get("/users.php").mock(return_value=httpx.Response(404))
        result = await get_profile(uid=1, ctx=ctx)
        assert "No profile found" in result

    async def test_users_200_missing_uid_returns_no_profile_found(self, ctx, api_mock):
        api_mock.get("/users.php").mock(return_value=httpx.Response(200, json={}))
        result = await get_profile(uid=1, ctx=ctx)
        assert "No profile found" in result


class TestGetProfileEnrichmentDegradation:
    async def test_certifications_404_degrades_to_empty_list(self, ctx, api_mock):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload())
        )
        api_mock.get("/certifications.php").mock(return_value=httpx.Response(404))
        api_mock.get("/languages/index.php").mock(
            return_value=httpx.Response(
                200, json={"results": [{"name": "English", "code": "en"}]}
            )
        )
        result = await get_profile(uid=42, ctx=ctx)
        assert "Certifications: None" in result
        assert "Languages:     English (en)" in result

    async def test_languages_404_degrades_to_empty_list(self, ctx, api_mock):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload())
        )
        api_mock.get("/certifications.php").mock(
            return_value=httpx.Response(200, json={"results": [{"name": "AWS"}]})
        )
        api_mock.get("/languages/index.php").mock(return_value=httpx.Response(404))
        result = await get_profile(uid=42, ctx=ctx)
        assert "Languages:     None" in result
        assert "AWS" in result

    async def test_both_enrichment_404s_still_render_users_profile(self, ctx, api_mock):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload())
        )
        api_mock.get("/certifications.php").mock(return_value=httpx.Response(404))
        api_mock.get("/languages/index.php").mock(return_value=httpx.Response(404))
        result = await get_profile(uid=42, ctx=ctx)
        assert "Name:          Ada Lovelace" in result
        assert "Certifications: None" in result
        assert "Languages:     None" in result


class TestGetProfileSuccess:
    async def test_combines_all_three_endpoints_and_strips_disallowed_fields(
        self, ctx, api_mock
    ):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(
                200,
                json=_users_payload(
                    internal_admin_notes="SECRET DO NOT LEAK",
                    email="ada@example.com",
                ),
            )
        )
        api_mock.get("/certifications.php").mock(
            return_value=httpx.Response(
                200, json={"results": [{"name": "AWS Cert", "category": "Cloud"}]}
            )
        )
        api_mock.get("/languages/index.php").mock(
            return_value=httpx.Response(
                200, json={"results": [{"name": "English", "code": "en"}]}
            )
        )
        result = await get_profile(uid=42, ctx=ctx)

        assert "Name:          Ada Lovelace" in result
        assert "Title:         Engineer" in result
        assert "Department:    R&D" in result
        assert "Location:      London, UK" in result
        assert "Skills:        Math" in result
        assert "Interests:     Poetry" in result
        assert "Groups:        Founders" in result
        assert "Languages:     English (en)" in result
        assert "AWS Cert (Cloud)" in result
        assert "Email:             ada@example.com" in result
        assert "SECRET DO NOT LEAK" not in result
        assert "internal_admin_notes" not in result

    async def test_uid_not_present_anywhere_in_output(self, ctx, api_mock):
        api_mock.get("/users.php").mock(
            return_value=httpx.Response(200, json=_users_payload(uid=999999))
        )
        api_mock.get("/certifications.php").mock(return_value=httpx.Response(404))
        api_mock.get("/languages/index.php").mock(return_value=httpx.Response(404))
        result = await get_profile(uid=999999, ctx=ctx)
        assert "999999" not in result
