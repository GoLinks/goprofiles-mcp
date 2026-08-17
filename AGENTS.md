# AGENTS.md

Orientation for an AI coding agent working in this repo — the decisions and
conventions that aren't visible from reading any single file. For anything
about project setup, running the server, or Docker, see `README.md`.

## Layout

```
src/goprofiles_mcp/
  client.py          shared HTTP client + helpers (auth header, error mapping, param tagging)
  confirmations.py   stage()/claim() write-confirmation mechanism (see below)
  server.py          builds the FastMCP app, registers every tool + OAuth scopes,
                      custom routes (health, OAuth/OIDC discovery)
  tools/
    _template.py     copy this to scaffold a new tool module — read it first
    bravos.py, celebrations.py, availability.py, people.py, meetings.py
tests/
  conftest.py        shared fixtures (see Testing below)
  tools/test_<name>.py   mirrors src/goprofiles_mcp/tools/<name>.py 1:1
```

## Adding a tool

Copy `src/goprofiles_mcp/tools/_template.py` — its docstring is the primary
reference for a tool module's shape (Field descriptions written for the model,
pydantic models that default every field, the `async def ... ctx: Context |
None = None` signature). Don't duplicate that here; two places documenting the
same shape just drift apart. Beyond what the template covers:

- Register it in `server.py` via `_oauth2_tool(...)`, passing the **narrowest**
  OAuth scope(s) the underlying PHP endpoint needs — this is what lets ChatGPT
  show per-tool scopes instead of the full server scope list.
- Add `tests/tools/test_<name>.py` in the same change. The mapping to
  `src/goprofiles_mcp/tools/<name>.py` is 1:1 — don't fold a new module's tests
  into an existing file or vice versa.
- If the tool copies fields out of a raw API payload into a pydantic model,
  check whether an explicit allow-list is warranted (see `people.py`'s
  `_USER_SCALAR_FIELDS` / `_allow_fields`) — that pattern exists to keep
  internal/sensitive API fields from ever reaching the model, not as
  incidental filtering. Skipping it on a new endpoint that returns more than
  it should is a real leak, not a style nit.
- If the tool renders another person's profile data into text a *third party*
  will see (a calendar invite, an email, anything beyond this one
  conversation), HTML-escape it — see `meetings.py`'s `_person_block`. A
  profile field is attacker-controlled from that third party's perspective:
  without escaping, someone's own `title` or `username` could inject markup
  into an invite a completely different person books with them.

## Write tools (mutating actions)

Every write goes through `confirmations.py`'s `stage()` / `claim()` pair so a
single tool call can never mutate anything — see `bravos.preview_bravo` /
`create_bravo` or `meetings.preview_meeting` / `schedule_meeting` for the
pattern:

1. A `preview_*` tool resolves ids to human-readable values, calls `stage()`
   with `payload` (what will execute, keyed on resolved internal ids) and
   `confirm_args` (what the caller must resend, in human-readable form), and
   returns a preview for the user to approve.
2. The mutating tool calls `claim()` with the same `confirm_args`. It only
   proceeds on `ClaimStatus.OK`; `DECLINED` / `DRIFTED` / `EXPIRED` /
   `NOTHING_PENDING` must each produce a clear message and **must not** issue
   the write.
3. Execute using the staged `payload`, never the arguments resent to the
   confirming call — that's what makes the confirm step tamper-proof.

Gotcha when constructing `confirm_args`: whitespace gets normalized before
comparison (`_normalize()` collapses runs of whitespace), so a message
re-wrapped with different line breaks still matches. Don't rely on exact
whitespace in a confirm-arg diff test, and don't "fix" that normalization
without checking why it's there.

## Style conventions already established in this codebase

- Minimal comments. A comment exists only to explain a non-obvious *why*
  (a workaround, an API quirk, a security-relevant choice) — never to restate
  what the code does.
- No speculative abstraction. Three similar blocks are fine; don't extract a
  helper until it's actually reused.
- Prefer explicit fallback text (`"Unknown"`, `"None"`) over omitting a field,
  so tool output is predictable for the model to parse and relay.
- Internal ids (`uid`, `bid`) are marked in output as "tool use only — do not
  show to the user." Preserve that annotation on any new field like it.

## Testing

- `uv run pytest -q` runs everything; `uv run pytest tests/tools/test_x.py -v`
  for one file while iterating. `uv run ruff check .` and
  `uv run ruff format --check .` must also pass before considering work done.
- `pytest-asyncio` runs in `auto` mode (see `pyproject.toml`) — async test
  functions need no decorator.
- HTTP is never real. `tests/conftest.py`'s `api_mock` fixture wraps `respx`
  bound to `GOPROFILES_API_URL` with `assert_all_mocked=True` (respx's
  default) — an unmocked request raises rather than hitting the network.
  Register routes with `api_mock.get("/path.php").mock(return_value=...)`.
- The real `fastmcp.Context` needs a live server to construct, so tests use
  `conftest.py`'s `FakeContext` (fixtures `ctx` / `make_ctx`) — a duck-typed
  double covering only what tools actually touch: `request_context.request.headers`,
  `session_id`, `session.check_client_capability(...)`, `elicit(...)`. Extend
  it rather than reaching for the real `Context` if a test needs a new knob.
- Test files are grouped in classes mirroring the module's public surface
  (`TestSearchPeople`, `TestFormatProfile`, etc.). Unit-test pure
  helpers/formatters directly with no HTTP involved; reserve `api_mock` for
  the tool functions that actually make requests.
- For write tools, always assert the negative path issues **zero** HTTP calls
  (`route.calls.call_count == 0` or similar) for declined/drifted/expired/
  nothing-pending cases — that's the property the confirmation mechanism
  exists to guarantee.
- Avoid coupling a test to an implementation's exact internal call count
  (e.g. a fixed-length `iter([...])` fed to a monkeypatched `time.time`) where
  a small stateful fake would express intent more clearly and survive an
  unrelated refactor.
- A local `.env` (gitignored) commonly sets `GOPROFILES_API_URL` and
  `GOPROFILES_EXTERNAL_REQUEST=true`; both get read into module-level
  constants at import time. A test touching either must pin the value
  explicitly with `monkeypatch` rather than assume a default — CI has no
  `.env` at all, so an assumed default will pass locally and fail (or worse,
  silently test the wrong thing) in CI.
