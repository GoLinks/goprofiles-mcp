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

MCP clients should send a per-user GoLinks OAuth/API bearer token with each request:

```http
Authorization: Bearer YOUR_TOKEN
```

The MCP server forwards that header to `api.goprofiles.io`. GoLinks remains responsible for token validation, scope enforcement, refresh, storage, and revocation.

An OAuth client must be pre-registered in your GoProfiles workspace with:

- Allowed scopes: 
- Redirect URIs: the exact callback URL(s) your MCP client uses


## Local Development

This project uses Python `3.12` and [`uv`](https://docs.astral.sh/uv/).

1. Install Python `3.12`.
2. Install `uv`.
3. Create the local environment and install dependencies:

```bash
uv sync
```

4. Run the MCP server locally:

```bash
uv run python -m goprofiles_mcp
```

The server binds to `0.0.0.0:8000` by default. Override with `MCP_HOST` and `MCP_PORT` if you need a different host or port:

```bash
MCP_HOST=127.0.0.1 MCP_PORT=9000 uv run python -m goprofiles_mcp
```

5. Verify health and OAuth discovery:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/.well-known/oauth-authorization-server
```

## Docker

Build and run locally:

```bash
docker build -t goprofiles-mcp .
docker run --rm -p 8000:8000 goprofiles-mcp
```

## Tools

| Tool             | Description                                | Scope           |
| ---------------- | ------------------------------------------ | --------------- |
| `EX_TOOL`   | `EX_DESC`     | `EX_SCOPE`  |

