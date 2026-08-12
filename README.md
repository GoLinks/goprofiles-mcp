# GoProfiles MCP Server

An MCP server that exposes [GoProfiles](https://www.goprofiles.io) as tools for AI assistants. Built with [FastMCP](https://gofastmcp.com).

## Hosted HTTP Mode

This server is intended to run as a hosted remote MCP server over Streamable HTTP.

Public endpoint shape:

```text
https://mcp.goprofiles.io/mcp
```

Local development endpoint shape:

```text
http://localhost:8000/mcp
```

## Authentication

The hosted server does not use a shared `GOPROFILES_API_TOKEN`.

MCP clients should send a per-user GoProfiles OAuth/API bearer token with each request:

```http
Authorization: Bearer YOUR_TOKEN
```

The MCP server forwards that header to the configured GoProfiles API
(`GOPROFILES_API_URL`, default `https://api.goprofiles.io`). GoProfiles remains responsible for token validation, scope enforcement, refresh, storage, and revocation.

An OAuth client must be pre-registered in your GoProfiles workspace with:

- Allowed scopes: `profiles:read`, `profiles:write`, `search:read`, `users:read`, `bravos:read`, `bravos:write`
- Redirect URIs: the exact callback URL(s) your MCP client uses

## Local Development

This project uses Python `3.12` and [`uv`](https://docs.astral.sh/uv/).

1. Install Python `3.12`.
2. Install `uv`.
3. Create the local environment and install dependencies:

```bash
uv sync
```

4. Point the server at your GoProfiles sandbox API (not production). Copy the
   example env file and replace `<your-username>` with your dev path:

```bash
cp .env.example .env
```

See [`.env.example`](.env.example) for every supported variable. `.env` is
gitignored and must not be committed. Without a local `.env`, the server
defaults to `https://api.goprofiles.io`.

5. Run the MCP server locally:

```bash
uv run python -m goprofiles_mcp
```

Confirm startup prints your sandbox base, e.g.
`GoProfiles API base: https://dev.goprofiles.io/d/<you>/d/api`.

The server binds to `0.0.0.0:8000` by default. Override with `MCP_HOST` and `MCP_PORT` if you need a different host or port:

```bash
MCP_HOST=127.0.0.1 MCP_PORT=9000 uv run python -m goprofiles_mcp
```

6. Verify health and OAuth discovery:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/.well-known/oauth-authorization-server
```

## Test on ChatGPT

ChatGPT cannot reach `localhost`. Expose your local MCP server with a public HTTPS tunnel (ngrok), then add it as a custom connector.

1. Start the MCP server locally (see [Local Development](#local-development)) so it is listening on port `8000`.
2. Install [ngrok](https://ngrok.com/download) (macOS example):

```bash
brew install ngrok
```

3. Sign up at [ngrok.com](https://ngrok.com), copy your authtoken, and configure the agent:

```bash
ngrok config add-authtoken YOUR_AUTHTOKEN
```

4. In a separate terminal, tunnel port `8000`:

```bash
ngrok http 8000
```

5. Copy the HTTPS forwarding URL ngrok prints (for example `https://abc123.ngrok-free.app`) and append `/mcp`:

```text
https://abc123.ngrok-free.app/mcp
```

6. Confirm the tunnel reaches your server:

```bash
curl -i https://abc123.ngrok-free.app/health
```

7. In ChatGPT: **Settings → Connectors**, enable **Developer Mode**, then create a connector and paste the `/mcp` URL from step 5.

**Notes**

- Keep both the MCP server and the ngrok process running while you test.
- `/mcp` requires a Bearer token. Full ChatGPT OAuth login needs working GoProfiles authorize/token endpoints; without that, the connector may reach the URL but fail authentication.
- A successful connect lists the tools in the [Tools](#tools) table below.

## Docker

Build and run locally:

```bash
docker build -t goprofiles-mcp .
docker run --rm -p 8000:8000 goprofiles-mcp
```

## Tools

Tool modules live under `src/goprofiles_mcp/tools/` and are registered in `src/goprofiles_mcp/server.py`. Copy `tools/_template.py` when adding a new one.

| Tool | Description | Scope |
| ---- | ----------- | ----- |
| `search_people` | Search and filter the people directory by name, department, title, location, language, skill, interest, and group. Resolves a name to the `uid` other tools need. | `search:read` |
| `get_profile` | Full profile for one `uid`: bio, title, contact info, skills, interests, groups, languages, certifications. | `profiles:read` |
| `search_celebrations` | Recent and upcoming birthdays, work anniversaries, and new hires in one merged feed, filterable by type and day window. | `profiles:read` |
| `get_availability` | Whether one `uid` is available right now: local time, working hours, current meeting, open blocks left today, and next time off. Combines the workspace's connected calendar (Google or Outlook) with their configured working hours. | `profiles:read` |
| `search_bravo_types` | List the Bravo badge types that can be given in the workspace. Resolves a badge name to the `bid` `preview_bravo` needs. | `bravos:read` |
| `preview_bravo` | Resolve a recipient `uid` and badge `bid` to real names and stage a Bravo for sending. Sends nothing. | `bravos:read`, `profiles:read` |
| `create_bravo` | Send a staged Bravo the user has explicitly approved. Sender is the authenticated user. Irreversible. | `bravos:write`, `profiles:read` |
| `preview_meeting` | Resolve an attendee `uid`, detect the workspace's calendar provider, and stage a calendar invite. Creates nothing. | `profiles:read` |
| `schedule_meeting` | Create a staged calendar invite the user has explicitly approved. Organizer is the authenticated user. Irreversible — the event lands on both calendars and the invite is emailed. | `profiles:write`, `profiles:read` |
