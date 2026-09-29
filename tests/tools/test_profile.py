from __future__ import annotations

import httpx
import pytest

from goprofiles_mcp import confirmations
from goprofiles_mcp.tools.profile import (
    CertificationAdd,
    _build_ops,
    _collect_args,
    preview_update_my_profile,
    update_my_profile,
)


@pytest.fixture(autouse=True)
def _clear_pending():
    with confirmations._lock:
        confirmations._pending.clear()
    yield
    with confirmations._lock:
        confirmations._pending.clear()


def _write_routes(api_mock):
    """Mock every profile write path the confirm tool might hit."""
    return {
        "users_put": api_mock.put("/users.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
        "skills_post": api_mock.post("/skills/user_skills.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
        "skills_delete": api_mock.request("DELETE", "/skills/user_skills.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
        "certs_post": api_mock.post("/certifications.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
        "certs_delete": api_mock.request("DELETE", "/certifications.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
        "langs_post": api_mock.post("/languages/users.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
        "langs_delete": api_mock.request("DELETE", "/languages/users.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        ),
    }


def _total_write_calls(routes: dict) -> int:
    return sum(route.calls.call_count for route in routes.values())


class TestCollectArgs:
    def test_requires_at_least_one_change(self):
        _, error = _collect_args(None, None, None, None, None, None, None)
        assert error is not None
        assert "at least one change" in error

    def test_rejects_bad_issue_date(self):
        _, error = _collect_args(
            None,
            None,
            None,
            [CertificationAdd(name="AWS", issue_date="01-02-2024")],
            None,
            None,
            None,
        )
        assert error is not None
        assert "YYYY-MM-DD" in error

    def test_rejects_add_and_remove_same_skill(self):
        _, error = _collect_args(None, ["Python"], ["python"], None, None, None, None)
        assert error is not None
        assert "both added and removed" in error

    def test_builds_ops_for_mixed_changes(self):
        args, error = _collect_args(
            "Hello",
            ["Python"],
            ["Java"],
            [CertificationAdd(name="AWS", issue_date="2024-01-15")],
            ["Old Cert"],
            ["es"],
            ["fr"],
        )
        assert error is None
        ops = _build_ops(args)
        methods_paths = [(op["method"], op["path"]) for op in ops]
        assert ("PUT", "/users.php") in methods_paths
        assert ("POST", "/skills/user_skills.php") in methods_paths
        assert ("DELETE", "/skills/user_skills.php") in methods_paths
        assert ("POST", "/certifications.php") in methods_paths
        assert ("DELETE", "/certifications.php") in methods_paths
        assert ("POST", "/languages/users.php") in methods_paths
        assert ("DELETE", "/languages/users.php") in methods_paths


class TestPreviewUpdateMyProfile:
    async def test_missing_ctx_raises(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await preview_update_my_profile(intro="hi", ctx=None)

    async def test_empty_change_set_does_not_stage(self, ctx):
        result = await preview_update_my_profile(ctx=ctx)
        assert "at least one change" in result
        assert confirmations._pending == {}

    async def test_successful_preview_stages_and_returns_text(self, ctx):
        result = await preview_update_my_profile(
            intro="I build tools.",
            skills_add=["Python"],
            skills_remove=["COBOL"],
            certifications_add=[
                CertificationAdd(name="AWS Cert", issue_date="2024-06-01")
            ],
            languages_add=["es"],
            ctx=ctx,
        )
        assert "Profile update previewed — NOT applied." in result
        assert "I build tools." in result
        assert "Skills to add:    Python" in result
        assert "Skills to remove: COBOL" in result
        assert "AWS Cert" in result
        assert "issued 2024-06-01" in result
        assert "Languages to add:    es" in result
        assert confirmations._pending != {}


class TestUpdateMyProfile:
    async def test_missing_ctx_raises(self):
        with pytest.raises(PermissionError, match="Missing request context"):
            await update_my_profile(intro="hi", ctx=None)

    async def test_nothing_pending_makes_no_writes(self, api_mock, ctx):
        routes = _write_routes(api_mock)
        result = await update_my_profile(intro="Hello", ctx=ctx)
        assert "no profile update waiting" in result
        assert _total_write_calls(routes) == 0

    async def test_declined_makes_no_writes(self, api_mock, make_ctx):
        routes = _write_routes(api_mock)
        preview_ctx = make_ctx()
        await preview_update_my_profile(intro="Hello", ctx=preview_ctx)

        decline_ctx = make_ctx(supports_elicitation=True, elicit_response=None)
        result = await update_my_profile(intro="Hello", ctx=decline_ctx)
        assert "user declined" in result
        assert _total_write_calls(routes) == 0

    async def test_drifted_makes_no_writes(self, api_mock, ctx):
        routes = _write_routes(api_mock)
        await preview_update_my_profile(intro="Hello", ctx=ctx)
        result = await update_my_profile(intro="Different bio", ctx=ctx)
        assert "do not match the preview" in result
        assert _total_write_calls(routes) == 0

    async def test_expired_makes_no_writes(self, api_mock, ctx):
        routes = _write_routes(api_mock)
        await preview_update_my_profile(intro="Hello", ctx=ctx)
        key = confirmations._owner_key(ctx, "update_my_profile")
        with confirmations._lock:
            confirmations._pending[key].expires_at = 0
        result = await update_my_profile(intro="Hello", ctx=ctx)
        assert "preview expired" in result
        assert _total_write_calls(routes) == 0

    async def test_happy_path_issues_expected_writes(self, api_mock, ctx):
        routes = _write_routes(api_mock)
        await preview_update_my_profile(
            intro="New bio",
            skills_add=["Python"],
            skills_remove=["Java"],
            certifications_add=[
                CertificationAdd(
                    name="AWS",
                    issue_date="2024-01-15",
                    category="Cloud",
                )
            ],
            certifications_remove=["Old"],
            languages_add=["es"],
            languages_remove=["fr"],
            ctx=ctx,
        )
        result = await update_my_profile(
            intro="New bio",
            skills_add=["Python"],
            skills_remove=["Java"],
            certifications_add=[
                CertificationAdd(
                    name="AWS",
                    issue_date="2024-01-15",
                    category="Cloud",
                )
            ],
            certifications_remove=["Old"],
            languages_add=["es"],
            languages_remove=["fr"],
            ctx=ctx,
        )

        assert "Profile updated successfully" in result
        assert routes["users_put"].calls.call_count == 1
        assert routes["skills_post"].calls.call_count == 1
        assert routes["skills_delete"].calls.call_count == 1
        assert routes["certs_post"].calls.call_count == 1
        assert routes["certs_delete"].calls.call_count == 1
        assert routes["langs_post"].calls.call_count == 1
        assert routes["langs_delete"].calls.call_count == 1

        put_body = httpx.QueryParams(routes["users_put"].calls.last.request.read())
        assert put_body["intro"] == "New bio"
        assert "uid" not in put_body

        skill_add = httpx.QueryParams(routes["skills_post"].calls.last.request.read())
        assert skill_add["name"] == "Python"

        cert_add = httpx.QueryParams(routes["certs_post"].calls.last.request.read())
        assert cert_add["name"] == "AWS"
        assert cert_add["issue_date"] == "2024-01-15"
        assert cert_add["category"] == "Cloud"

        lang_add = httpx.QueryParams(routes["langs_post"].calls.last.request.read())
        assert lang_add["code"] == "es"

    async def test_partial_failure_stops_and_reports_applied(self, api_mock, ctx):
        users_put = api_mock.put("/users.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        skills_post = api_mock.post("/skills/user_skills.php").mock(
            return_value=httpx.Response(422, text="locked by data matching")
        )
        langs_post = api_mock.post("/languages/users.php").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        await preview_update_my_profile(
            intro="Bio",
            skills_add=["Python"],
            languages_add=["es"],
            ctx=ctx,
        )
        result = await update_my_profile(
            intro="Bio",
            skills_add=["Python"],
            languages_add=["es"],
            ctx=ctx,
        )

        assert "partially applied" in result
        assert "bio" in result
        assert users_put.calls.call_count == 1
        assert skills_post.calls.call_count == 1
        # Stopped before languages after skills failed.
        assert langs_post.calls.call_count == 0
