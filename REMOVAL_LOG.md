# GoLinks → GoProfiles blank-slate removal log

This repo started as a copy of the GoLinks MCP server. The items below were
removed or rewritten so the project is a runnable GoProfiles MCP shell with
shared HTTP/OAuth infrastructure and **no product tools**.

## Deleted tool modules

| File | What it provided | Why removed |
| ---- | ---------------- | ----------- |
| `src/goprofiles_mcp/tools/golinks.py` | `list_golinks`, `get_golink`, `create_golink` against `/golinks` | GoLinks-specific CRUD; no GoProfiles equivalent yet |
| `src/goprofiles_mcp/tools/search.py` | `search_golinks` against `/search.php` (`result-type=links`) | Go link search filters/sorts/models are product-specific |
| `src/goprofiles_mcp/tools/collections.py` | `search_collections` against `/search.php` (`result-type=collections`) | GoLinks collections API and department taxonomy |
| `src/goprofiles_mcp/tools/users.py` | `search_users` against `/users` | GoLinks user fields (e.g. go-link counts) and scopes |
| `src/goprofiles_mcp/tools/audit_log.py` | `get_audit_logs` against `/admin/audit_log` | GoLinks audit event types/sections (GoLink*, Collections, Jots, etc.) |

## Removed from `server.py`

| Item | Why removed |
| ---- | ----------- |
| Imports and `mcp.add_tool(...)` for all seven GoLinks tools | Leave a blank tool registry for GoProfiles tools |
| GoLinks OAuth scope list (`golinks:read`, `golinks:write`, `search:read`, `admin:read`, `users:read`) | Scopes are product-specific; left `_SCOPES = []` until GoProfiles scopes are defined |
| `/.well-known/openai-apps-challenge` route and hardcoded challenge token | GoLinks OpenAI Apps verification artifact; re-add for GoProfiles if/when needed |
| FastMCP app name `"GoLinks"` | Renamed to `"GoProfiles"` |
| `GOLINKS_*` OAuth env defaults and `mcp.golinks.io` resource URL | Replaced with `GOPROFILES_*` / `mcp.goprofiles.io` placeholders |
| WWW-Authenticate realm `"GoLinks MCP"` | Renamed to `"GoProfiles MCP"` |

## Rewritten shared client (`client.py`)

| Change | Why |
| ------ | --- |
| `GOLINKS_API_URL` → `GOPROFILES_API_URL` (default `https://api.goprofiles.io`) | Point HTTP client at GoProfiles API |
| `GOLINKS_EXTERNAL_REQUEST` → `GOPROFILES_EXTERNAL_REQUEST` | Match new env naming |
| Error messages saying "GoLinks" → "GoProfiles" | Avoid wrong product branding in failures |
| Removed `golink_path()` helper | Only meaningful for go/ / go/my/ URL paths |

Kept (intentionally): `http_client`, `external_params`, `raise_for_status`, `format_timestamp`, `get_authorization_header`, `SortOrder` — reusable for future GoProfiles tools.

## Package / entrypoint rebrand

| Change | Why |
| ------ | --- |
| Imports `golinks_mcp.*` → `goprofiles_mcp.*` in `__main__.py` / `server.py` | Match on-disk package name |
| `pyproject.toml` name/description → `goprofiles-mcp` / GoProfiles | Correct package identity |
| Default `MCP_RESOURCE_METADATA_URL` → `mcp.goprofiles.io/...` | Match hosted endpoint shape |
| README auth copy still saying "GoLinks" → "GoProfiles" | Leftover from incomplete rename |
| `cspell.json` words `golinks`/`golink` → `goprofiles`/`goprofile` | Spellcheck for new product name |

## What remains (blank-slate skeleton)

- Streamable HTTP MCP host (`__main__.py`)
- Bearer-required middleware on `/mcp`
- Health + OAuth discovery well-known routes
- Shared API client helpers for upcoming tools
- Empty `tools/` package ready for new modules
