from __future__ import annotations

from urllib.parse import parse_qsl

import httpx
import pytest
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

from goprofiles_mcp import confirmations
from goprofiles_mcp.tools.my_profile import (
    CustomFieldChange,
    EditableProfile,
    LocationChange,
    ProfileChanges,
    _confirm_summary,
    _form_fields,
    _format_profile,
    _profile_from_user,
    _Refusal,
    _resolve_changes,
    get_my_profile,
    preview_my_profile_update,
    update_my_profile,
)


@pytest.fixture(autouse=True)
def _clear_pending():
    """The confirmations store is process-global — reset it around each test."""
    with confirmations._lock:
        confirmations._pending.clear()
    yield
    with confirmations._lock:
        confirmations._pending.clear()


def _open(editable=True, reason=None, locked_by=None):
    return {"editable": editable, "locked_reason": reason, "locked_by": locked_by}


def _user_payload():
    """users.php GET ?me — getUserInfo() plus editable_fields."""
    return {
        "uid": 42,
        "username": "karan",
        "first_name": "Karan",
        "last_name": "Komal",
        "title": "Engineer",
        "department": "Engineering",
        "pronouns": "he/him",
        "intro": "Old bio",
        "phone": "5125550100",
        "phone_iso2": "US",
        "personal_phone": None,
        "personal_phone_iso2": None,
        "birthday": "00-00",
        "city": "Austin",
        "state": "Texas",
        "country": "United States",
        "timezone": "America/Chicago",
        "linkedin": None,
        "twitter": None,
        "github": "karan",
        "personal_website": None,
        "bio_enabled": "1",
        "social_enabled": "1",
        "personal_phone_enabled": "0",
        "bio_fields": [
            {
                "ufid": 9012,
                "name": "Favorite book",
                "field_type": "text",
                "instructions": "One title only",
                "example": None,
                "value": "Dune",
            },
            {
                "ufid": 9013,
                "name": "T-shirt size",
                "field_type": "dropdown",
                "instructions": None,
                "example": None,
                "selected": ["M"],
                "options": ["S", "M", "L"],
            },
            {
                "ufid": 9014,
                "name": "Hobbies",
                "field_type": "multi choice",
                "instructions": None,
                "example": None,
                "selected": ["Chess"],
                "options": ["Hiking", "Chess", "Cooking"],
            },
        ],
        "editable_fields": {
            "first_name": _open(),
            "last_name": _open(),
            "title": _open(False, "synced", "HRIS"),
            "department": _open(False, "admin_managed"),
            "pronouns": _open(),
            "intro": _open(),
            "phone": _open(),
            "personal_phone": _open(False, "disabled"),
            "birthday": _open(),
            "location": _open(),
            "linkedin": _open(),
            "twitter": _open(),
            "github": _open(),
            "personal_website": _open(),
            "bio_fields": _open(),
        },
    }


def _profile(payload=None, departments=()):
    return _profile_from_user(payload or _user_payload(), list(departments))


def _mock_get(api_mock, payload=None):
    return api_mock.get("/users.php").mock(
        return_value=httpx.Response(200, json=payload or _user_payload())
    )


class TestProfileFromUser:
    def test_editability_comes_from_editable_fields(self):
        profile = _profile()

        assert profile.fields["intro"].editable is True
        assert profile.fields["title"].locked_reason == "synced"
        assert profile.fields["title"].locked_by == "HRIS"

    def test_field_missing_from_editable_fields_fails_closed(self):
        payload = _user_payload()
        del payload["editable_fields"]["github"]

        assert _profile(payload).fields["github"].editable is False

    def test_server_without_editable_fields_locks_everything(self):
        payload = _user_payload()
        del payload["editable_fields"]

        profile = _profile(payload)

        assert not any(f.editable for f in profile.fields.values())
        assert not any(c.editable for c in profile.custom_fields)

    def test_unset_birthday_reads_as_none(self):
        assert _profile().fields["birthday"].value is None

    def test_custom_fields_take_selected_values_and_bio_lock(self):
        payload = _user_payload()
        payload["editable_fields"]["bio_fields"] = _open(False, "disabled")

        by_name = {c.name: c for c in _profile(payload).custom_fields}

        assert by_name["T-shirt size"].value == "M"
        assert by_name["Hobbies"].value == ["Chess"]
        assert by_name["Favorite book"].options is None
        assert not by_name["Favorite book"].editable
        assert by_name["Favorite book"].locked_reason == "disabled"

    def test_html_escaped_text_is_unescaped(self):
        payload = _user_payload()
        payload["department"] = "R&amp;D"

        assert _profile(payload).fields["department"].value == "R&D"


class TestFormatProfile:
    def test_splits_editable_and_locked_with_reasons(self):
        text = _format_profile(_profile())
        editable, locked = text.split("Locked (cannot be changed from here):")

        assert "Bio [bio]: Old bio" in editable
        assert "Work phone [work_phone]: 5125550100 (US)" in editable
        assert "Austin, Texas, United States (timezone America/Chicago)" in editable
        assert "Birthday [birthday]: None" in editable
        assert (
            "Job title [title]: Engineer — synced from HRIS — change it there" in locked
        )
        assert "only a workspace admin can change this" in locked
        assert "Personal phone [personal_phone]: None — turned off" in locked

    def test_custom_fields_show_type_options_and_value_but_not_ufid(self):
        text = _format_profile(_profile())

        assert "Favorite book (text): Dune" in text
        assert "Hint: One title only" in text
        assert "T-shirt size (dropdown; options: S, M, L): M" in text
        assert "Hobbies (multi choice; options: Hiking, Chess, Cooking): Chess" in text
        assert "9012" not in text and "ufid" not in text

    def test_empty_profile_says_none_rather_than_omitting_sections(self):
        text = _format_profile(EditableProfile())

        assert text.count("  None") == 3


class TestResolveChanges:
    def test_builds_users_php_body(self):
        resolved = _resolve_changes(
            ProfileChanges(
                bio="New bio",
                work_phone="(512) 555-0199",
                work_phone_country="us",
                location=LocationChange(city="Denver", country="United States"),
            ),
            _profile(),
        )

        assert resolved.body == {
            "intro": "New bio",
            "phone": "5125550199",
            "phone_iso2": "US",
            "city": "Denver",
            "state": "",
            "country": "United States",
        }
        assert "Bio: Old bio → New bio" in resolved.lines
        assert (
            "Location: Austin, Texas, United States → Denver, United States"
            in resolved.lines
        )

    def test_empty_string_clears_a_field(self):
        resolved = _resolve_changes(ProfileChanges(github=""), _profile())

        assert resolved.body == {"github": ""}
        assert resolved.lines == ["GitHub: karan → (cleared)"]

    def test_cleared_birthday_is_sent_as_the_stored_default(self):
        resolved = _resolve_changes(ProfileChanges(birthday=""), _profile())

        assert resolved.body == {"birthday": "00-00"}

    def test_phone_is_saved_and_previewed_as_its_digits(self):
        resolved = _resolve_changes(
            ProfileChanges(work_phone="(512) 555-0199"), _profile()
        )

        assert resolved.body == {"phone": "5125550199"}
        assert resolved.lines == ["Work phone: 5125550100 (US) → 5125550199"]

    def test_website_without_scheme_gets_https_like_users_php(self):
        resolved = _resolve_changes(
            ProfileChanges(personal_website="komal.io"), _profile()
        )

        assert resolved.body == {"personal_website": "https://komal.io"}
        assert resolved.lines == ["Personal website: None → https://komal.io"]

    def test_custom_phone_and_url_fields_keep_their_country_and_label(self):
        payload = _user_payload()
        payload["bio_fields"] += [
            {
                "ufid": 9020,
                "name": "Desk phone",
                "field_type": "phone",
                "value": "2079460958",
                "iso2": "GB",
            },
            {
                "ufid": 9021,
                "name": "Portfolio",
                "field_type": "url",
                "value": "https://a.example",
                "url_name": "My work",
            },
        ]

        resolved = _resolve_changes(
            ProfileChanges(
                custom_fields=[
                    CustomFieldChange(name="Desk phone", value="2079460000"),
                    CustomFieldChange(name="Portfolio", value="https://b.example"),
                ]
            ),
            _profile(payload),
        )

        assert resolved.body["bio_fields_text"] == [
            {"ufid": 9020, "value": "2079460000", "iso2": "GB"},
            {"ufid": 9021, "value": "https://b.example", "url_name": "My work"},
        ]

    def test_stored_values_are_sent_back_verbatim_not_unescaped(self):
        payload = _user_payload()
        payload["first_name"] = "Tom &amp; Co"
        payload["bio_fields"][1]["options"] = ["S", "M &amp; L"]
        payload["bio_fields"][1]["selected"] = ["M &amp; L"]

        resolved = _resolve_changes(
            ProfileChanges(
                last_name="Smith",
                custom_fields=[CustomFieldChange(name="T-shirt size", value="S")],
            ),
            _profile(payload),
        )

        # The paired name must equal the stored value, or users.php sees a change.
        assert resolved.body["first_name"] == "Tom &amp; Co"
        assert "T-shirt size: M &amp; L → S" in resolved.lines

    def test_one_name_part_sends_both_because_users_php_needs_the_pair(self):
        resolved = _resolve_changes(ProfileChanges(last_name="Smith"), _profile())

        assert resolved.body == {"last_name": "Smith", "first_name": "Karan"}
        assert resolved.lines == ["Last name: Komal → Smith"]

    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            (ProfileChanges(title="Staff Engineer"), "Job title — synced from HRIS"),
            (ProfileChanges(department="Sales"), "only a workspace admin"),
            (ProfileChanges(personal_phone="5125550123"), "turned off"),
        ],
    )
    def test_locked_field_is_refused_with_reason(self, changes, expected):
        with pytest.raises(_Refusal) as exc:
            _resolve_changes(changes, _profile())

        assert expected in str(exc.value)

    def test_one_locked_field_refuses_the_whole_change_set(self):
        with pytest.raises(_Refusal) as exc:
            _resolve_changes(
                ProfileChanges(bio="New", title="Staff Engineer"), _profile()
            )

        assert "Job title" in str(exc.value)
        assert "Bio" not in str(exc.value)

    def test_custom_fields_resolve_by_name_to_the_users_php_arrays(self):
        resolved = _resolve_changes(
            ProfileChanges(
                custom_fields=[
                    CustomFieldChange(name="favorite BOOK", value="Neuromancer"),
                    CustomFieldChange(name="T-shirt size", value="L"),
                    CustomFieldChange(name="Hobbies", value=["Hiking", "Chess"]),
                ]
            ),
            _profile(),
        )

        assert resolved.body == {
            "bio_fields_text": [{"ufid": 9012, "value": "Neuromancer"}],
            "bio_fields_dropdown": [{"ufid": 9013, "selection": "L"}],
            "bio_fields_multi_choice": [
                {"ufid": 9014, "selected": ["Hiking", "Chess"]}
            ],
        }
        assert resolved.custom_field_names == {
            9012: "Favorite book",
            9013: "T-shirt size",
            9014: "Hobbies",
        }
        assert "Hobbies: Chess → Hiking, Chess" in resolved.lines

    def test_bio_turned_off_refuses_custom_fields(self):
        payload = _user_payload()
        payload["editable_fields"]["bio_fields"] = _open(False, "disabled")

        with pytest.raises(_Refusal) as exc:
            _resolve_changes(
                ProfileChanges(
                    custom_fields=[CustomFieldChange(name="Hobbies", value=[])]
                ),
                _profile(payload),
            )

        assert "Hobbies — turned off" in str(exc.value)

    def test_unknown_custom_field_lists_the_real_ones(self):
        with pytest.raises(_Refusal) as exc:
            _resolve_changes(
                ProfileChanges(
                    custom_fields=[CustomFieldChange(name="Pet", value="x")]
                ),
                _profile(),
            )

        assert "Favorite book, T-shirt size, Hobbies" in str(exc.value)

    @pytest.mark.parametrize(
        ("name", "value"),
        [("T-shirt size", "XXL"), ("Hobbies", ["Chess", "Skydiving"])],
    )
    def test_value_outside_options_is_refused(self, name, value):
        with pytest.raises(_Refusal) as exc:
            _resolve_changes(
                ProfileChanges(
                    custom_fields=[CustomFieldChange(name=name, value=value)]
                ),
                _profile(),
            )

        assert "is not an option" in str(exc.value)

    def test_list_value_for_single_value_field_is_refused(self):
        with pytest.raises(_Refusal):
            _resolve_changes(
                ProfileChanges(
                    custom_fields=[CustomFieldChange(name="Favorite book", value=["a"])]
                ),
                _profile(),
            )

    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            (ProfileChanges(), "No changes"),
            (ProfileChanges(first_name=" "), "First name cannot be empty"),
            (ProfileChanges(work_phone_country="US"), "together with work_phone"),
            (
                ProfileChanges(work_phone="1", work_phone_country="USA"),
                "two-letter",
            ),
            (ProfileChanges(work_phone="call me"), "can only contain digits"),
            (ProfileChanges(work_phone="555-1234 ext 12"), "can only contain digits"),
            (ProfileChanges(work_phone="+44 20 7946 0958"), "without the '+'"),
            (ProfileChanges(birthday="July 14"), "MM-DD"),
            (ProfileChanges(bio="x" * 301), "at most 300 characters (this is 301)"),
            (ProfileChanges(pronouns="x" * 21), "at most 20"),
            (ProfileChanges(twitter="x" * 51), "at most 50"),
            (ProfileChanges(first_name="x" * 101), "at most 100"),
            (ProfileChanges(personal_website="not a url"), "not a valid URL"),
            (
                ProfileChanges(location=LocationChange(city="Denver")),
                "Include the country",
            ),
            (
                ProfileChanges(
                    custom_fields=[
                        CustomFieldChange(name="Hobbies", value=["Chess"]),
                        CustomFieldChange(name="hobbies", value=[]),
                    ]
                ),
                "listed more than once",
            ),
        ],
    )
    def test_malformed_changes_are_refused(self, changes, expected):
        with pytest.raises(_Refusal) as exc:
            _resolve_changes(changes, _profile())

        assert expected in str(exc.value)


class TestFormFields:
    def test_nested_bio_fields_flatten_to_php_keys(self):
        body = {
            "intro": "Hi",
            "bio_fields_dropdown": [{"ufid": 9013, "selection": "L"}],
            "bio_fields_multi_choice": [
                {"ufid": 9014, "selected": ["Hiking", "Chess"]},
                {"ufid": 9015, "selected": []},
            ],
        }

        assert _form_fields(body) == {
            "intro": "Hi",
            "bio_fields_dropdown[0][ufid]": "9013",
            "bio_fields_dropdown[0][selection]": "L",
            "bio_fields_multi_choice[0][ufid]": "9014",
            "bio_fields_multi_choice[0][selected][0]": "Hiking",
            "bio_fields_multi_choice[0][selected][1]": "Chess",
            # No `selected` key at all: users.php reads that as "deselect all".
            "bio_fields_multi_choice[1][ufid]": "9015",
        }


class TestConfirmSummary:
    def test_labels_values_like_the_preview(self):
        summary = _confirm_summary(
            ProfileChanges(
                bio="Hi",
                github="",
                work_phone="5125550199",
                work_phone_country="GB",
                location=LocationChange(city="Denver", country="United States"),
                custom_fields=[CustomFieldChange(name="Hobbies", value=["Chess"])],
            ).model_dump(exclude_none=True)
        )

        assert "Bio: Hi" in summary
        assert "GitHub: (cleared)" in summary
        assert "Work phone country: GB" in summary
        assert "Location: Denver, United States" in summary
        assert "Hobbies: Chess" in summary
        assert "{" not in summary

    def test_flags_department_creation_and_shows_saved_phone_digits(self):
        summary = _confirm_summary(
            ProfileChanges(department="Legal", work_phone="(512) 555-0199").model_dump(
                exclude_none=True
            )
        )

        assert "Department: Legal (created for the whole workspace" in summary
        assert "Work phone: 5125550199" in summary


class TestGetMyProfile:
    async def test_missing_ctx_raises(self):
        with pytest.raises(PermissionError):
            await get_my_profile(ctx=None)

    async def test_reads_self_from_users_php_without_uid(self, api_mock, ctx):
        route = _mock_get(api_mock)

        text = await get_my_profile(ctx=ctx)

        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer test-token"
        assert "me" in request.url.params
        assert request.url.params["mcp_tool"] == "get_my_profile"
        assert "uid" not in request.url.params
        assert "Your GoProfiles profile" in text

    async def test_never_lists_or_fetches_departments(self, api_mock, ctx):
        payload = _user_payload()
        payload["editable_fields"]["department"] = _open()
        _mock_get(api_mock, payload)
        departments = api_mock.get("/departments/index.php").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        text = await get_my_profile(ctx=ctx)

        assert departments.call_count == 0
        assert "Department [department]: Engineering" in text
        assert "Existing departments" not in text


class TestPreviewMyProfileUpdate:
    async def test_stages_resolved_body_and_shows_before_after(self, api_mock, ctx):
        _mock_get(api_mock)

        text = await preview_my_profile_update(ProfileChanges(bio="Hi there"), ctx=ctx)

        assert "NOT saved" in text
        assert "Bio: Old bio → Hi there" in text
        (pending,) = confirmations._pending.values()
        assert pending.payload["body"] == {"intro": "Hi there"}
        assert pending.confirm_args == {"bio": "Hi there"}

    async def test_locked_field_stages_nothing(self, api_mock, ctx):
        _mock_get(api_mock)

        text = await preview_my_profile_update(
            ProfileChanges(title="Staff Engineer"), ctx=ctx
        )

        assert text.startswith("No preview —")
        assert "synced from HRIS" in text
        assert confirmations._pending == {}


def _ok_put(failed=None):
    return httpx.Response(
        200, json={"failed_bio_updates": failed or [], "status": "ok"}
    )


async def _preview(api_mock, ctx, changes, payload=None):
    _mock_get(api_mock, payload)
    await preview_my_profile_update(changes, ctx=ctx)


def _mock_put(api_mock, response=None):
    return api_mock.put("/users.php").mock(return_value=response or _ok_put())


class TestUpdateMyProfile:
    async def test_missing_ctx_raises(self):
        with pytest.raises(PermissionError):
            await update_my_profile(ProfileChanges(bio="x"), ctx=None)

    async def test_nothing_pending_makes_no_put(self, api_mock, ctx):
        route = _mock_put(api_mock)

        text = await update_my_profile(ProfileChanges(bio="x"), ctx=ctx)

        assert "no profile update waiting" in text
        assert route.call_count == 0

    async def test_drifted_changes_make_no_put(self, api_mock, ctx):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        route = _mock_put(api_mock)

        text = await update_my_profile(ProfileChanges(bio="Sneaky bio"), ctx=ctx)

        assert "does not match the preview" in text
        assert route.call_count == 0

    async def test_extra_field_on_confirm_counts_as_drift(self, api_mock, ctx):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        route = _mock_put(api_mock)

        text = await update_my_profile(
            ProfileChanges(bio="Approved bio", github="someone-else"), ctx=ctx
        )

        assert "does not match the preview" in text
        assert route.call_count == 0

    async def test_declined_makes_no_put(self, api_mock, make_ctx):
        ctx = make_ctx(supports_elicitation=True, elicit_response=DeclinedElicitation())
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        route = _mock_put(api_mock)

        text = await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)

        assert "declined" in text
        assert route.call_count == 0

    async def test_expired_makes_no_put(self, api_mock, ctx, monkeypatch):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        route = _mock_put(api_mock)
        now = confirmations.time.time()
        monkeypatch.setattr(
            confirmations.time,
            "time",
            lambda: now + confirmations.DEFAULT_TTL_SECONDS + 1,
        )

        text = await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)

        assert "expired" in text
        assert route.call_count == 0

    async def test_confirmed_update_puts_staged_form_body(self, api_mock, make_ctx):
        ctx = make_ctx(
            supports_elicitation=True,
            elicit_response=AcceptedElicitation(data="Confirm"),
        )
        changes = ProfileChanges(
            bio="Approved\nbio",
            custom_fields=[CustomFieldChange(name="T-shirt size", value="L")],
        )
        await _preview(api_mock, ctx, changes)
        route = _mock_put(api_mock)

        # Re-wrapped whitespace is not drift.
        resent = ProfileChanges(
            bio="Approved bio",
            custom_fields=[CustomFieldChange(name="T-shirt size", value="L")],
        )
        text = await update_my_profile(resent, ctx=ctx)

        assert text == "Profile updated."
        request = route.calls.last.request
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.url.params["mcp_tool"] == "update_my_profile"
        form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        assert form == {
            "intro": "Approved\nbio",
            "bio_fields_dropdown[0][ufid]": "9013",
            "bio_fields_dropdown[0][selection]": "L",
        }
        # Self-only: no uid of any kind goes to the server.
        assert "uid" not in request.url.params
        assert "alter_uid" not in form and "uid" not in form

    async def test_claim_is_consumed_so_a_second_confirm_makes_no_put(
        self, api_mock, ctx
    ):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        route = _mock_put(api_mock)

        await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)
        text = await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)

        assert "no profile update waiting" in text
        assert route.call_count == 1

    async def test_failed_bio_updates_are_reported_by_name(self, api_mock, ctx):
        changes = ProfileChanges(
            bio="Approved bio",
            custom_fields=[
                CustomFieldChange(name="Favorite book", value="Neuromancer"),
                CustomFieldChange(name="Hobbies", value=["Hiking"]),
            ],
        )
        await _preview(api_mock, ctx, changes)
        _mock_put(
            api_mock,
            _ok_put(
                {
                    "text": [
                        {
                            "ufid": 9012,
                            "selection": "Neuromancer",
                            "error": "Bio field not found",
                        }
                    ]
                }
            ),
        )

        text = await update_my_profile(changes, ctx=ctx)

        assert text.startswith("Profile partly updated.")
        assert "Favorite book: Bio field not found" in text
        assert "Hobbies" not in text
        assert "Everything else in the preview was saved" in text

    async def test_location_update_reports_geocoded_value(self, api_mock, ctx):
        saved = _user_payload()
        saved.update(city="Denver", state="Colorado", timezone="America/Denver")
        get = api_mock.get("/users.php").mock(
            side_effect=[
                httpx.Response(200, json=_user_payload()),
                httpx.Response(200, json=saved),
            ]
        )
        changes = ProfileChanges(
            location=LocationChange(city="denver", country="United States")
        )
        await preview_my_profile_update(changes, ctx=ctx)
        _mock_put(api_mock)

        text = await update_my_profile(changes, ctx=ctx)

        assert get.call_count == 2
        assert (
            "Location saved as: Denver, Colorado, United States "
            "(timezone America/Denver)" in text
        )

    async def test_failed_reread_after_save_still_reports_success(self, api_mock, ctx):
        api_mock.get("/users.php").mock(
            side_effect=[
                httpx.Response(200, json=_user_payload()),
                httpx.Response(500, text="boom"),
            ]
        )
        changes = ProfileChanges(
            location=LocationChange(city="Denver", country="United States")
        )
        await preview_my_profile_update(changes, ctx=ctx)
        _mock_put(api_mock)

        text = await update_my_profile(changes, ctx=ctx)

        assert text.startswith("Profile updated.")
        assert "Couldn't re-read the saved location" in text

    @pytest.mark.parametrize("status", [400, 422])
    async def test_server_refusal_is_relayed_not_raised(self, api_mock, ctx, status):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        _mock_put(
            api_mock,
            httpx.Response(
                status,
                json={"error": {"message": "An error occurred", "type": "warn"}},
            ),
        )

        text = await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)

        assert text.startswith("No changes saved")
        assert "An error occurred" in text
        assert "may already have been saved" not in text

    async def test_403_with_error_message_raises_as_auth(self, api_mock, ctx):
        # e.g. Access::checkAPIAccess / loggedOut — not a users.php refusal.
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        _mock_put(
            api_mock,
            httpx.Response(403, json={"error": {"message": "An error occurred"}}),
        )

        with pytest.raises(PermissionError):
            await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)

    @pytest.mark.parametrize("status", [422, 500])
    async def test_refusal_after_custom_fields_warns_they_may_have_saved(
        self, api_mock, ctx, status
    ):
        changes = ProfileChanges(
            work_phone="5125550199",
            custom_fields=[CustomFieldChange(name="Favorite book", value="Emma")],
        )
        await _preview(api_mock, ctx, changes)
        _mock_put(api_mock, httpx.Response(status, json={"error": {"message": "x"}}))

        text = await update_my_profile(changes, ctx=ctx)

        assert text.startswith("Profile may be partly updated")
        assert "may already have been saved" in text
        assert "get_my_profile" in text

    async def test_missing_scope_raises_so_the_client_can_reauthorize(
        self, api_mock, ctx
    ):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        _mock_put(
            api_mock,
            httpx.Response(
                403,
                json={
                    "error": "insufficient_scope",
                    "error_description": "Insufficient scope.",
                },
            ),
        )

        with pytest.raises(PermissionError):
            await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)

    async def test_expired_token_still_raises(self, api_mock, ctx):
        await _preview(api_mock, ctx, ProfileChanges(bio="Approved bio"))
        _mock_put(api_mock, httpx.Response(401))

        with pytest.raises(PermissionError):
            await update_my_profile(ProfileChanges(bio="Approved bio"), ctx=ctx)


def _editable_department_payload():
    payload = _user_payload()
    payload["editable_fields"]["department"] = _open()
    return payload


def _mock_departments(api_mock, names):
    return api_mock.get("/departments/index.php").mock(
        return_value=httpx.Response(
            200, json={"results": [{"did": i, "name": n} for i, n in enumerate(names)]}
        )
    )


class TestDepartmentChanges:
    @pytest.mark.parametrize(
        ("typed", "stored"),
        [("customer  success", "Customer Success"), ("r & d", "R&D")],
    )
    def test_existing_department_matches_like_the_api_and_is_not_flagged(
        self, typed, stored
    ):
        resolved = _resolve_changes(
            ProfileChanges(department=typed),
            _profile(
                _editable_department_payload(),
                ["Customer Success", "Engineering", "R&D"],
            ),
        )

        assert resolved.body == {"department": typed}
        assert resolved.lines == [f"Department: Engineering → {stored}"]
        assert resolved.new_department is None

    def test_unknown_department_is_flagged_with_close_matches(self):
        resolved = _resolve_changes(
            ProfileChanges(department="Enginering"),
            _profile(_editable_department_payload(), ["Engineering"]),
        )

        assert "NEW — this creates a department" in resolved.lines[0]
        assert "ask whether they meant Engineering" in resolved.lines[-1]
        assert resolved.new_department == "Enginering"

    def test_unknown_department_with_no_close_match_still_warns(self):
        resolved = _resolve_changes(
            ProfileChanges(department="Legal"),
            _profile(_editable_department_payload(), ["Engineering"]),
        )

        assert "NEW" in resolved.lines[0]
        assert "ask whether they meant" not in resolved.lines[-1]
        assert "creates it for the whole workspace" in resolved.lines[-1]

    @pytest.mark.parametrize(
        ("changes", "editable", "fetched"),
        [
            (ProfileChanges(department="Sales"), True, 1),
            (ProfileChanges(bio="Hi"), True, 0),
            (ProfileChanges(department="Sales"), False, 0),
        ],
    )
    async def test_preview_fetches_departments_only_for_an_editable_change(
        self, api_mock, ctx, changes, editable, fetched
    ):
        payload = _user_payload()
        payload["editable_fields"]["department"] = _open(editable)
        departments = _mock_departments(api_mock, ["Sales"])

        await _preview(api_mock, ctx, changes, payload)

        assert departments.call_count == fetched

    async def test_suggestions_list_duplicate_names_once(self, api_mock, ctx):
        _mock_departments(api_mock, ["R&D", "R&amp;D", "R&D", "Engineering"])
        _mock_get(api_mock, _editable_department_payload())

        text = await preview_my_profile_update(
            ProfileChanges(department="R&E"), ctx=ctx
        )

        assert "ask whether they meant R&D." in text
        assert "R&D or R&D" not in text

    async def test_created_department_is_reported_after_save(self, api_mock, ctx):
        _mock_departments(api_mock, ["Engineering"])
        changes = ProfileChanges(department="Legal")
        await _preview(api_mock, ctx, changes, _editable_department_payload())
        api_mock.put("/users.php").mock(return_value=_ok_put())

        text = await update_my_profile(changes, ctx=ctx)

        assert "Created new department: Legal" in text
