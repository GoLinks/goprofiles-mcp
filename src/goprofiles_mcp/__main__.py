import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from starlette.middleware import Middleware

from goprofiles_mcp.client import GOPROFILES_API_URL
from goprofiles_mcp.server import RequireBearerOnMCP, mcp


def main() -> None:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    print(f"GoProfiles API base: {GOPROFILES_API_URL}", flush=True)
    resource_metadata_url = os.environ.get(
        "MCP_RESOURCE_METADATA_URL",
        "https://mcp.goprofiles.io/.well-known/oauth-protected-resource/mcp",
    )
    # Stateful sessions live in the memory of one task. That's fine as long as
    # there's exactly one running task and it isn't replaced mid-session — a
    # restart or a second task behind the ALB sends a session's follow-up
    # requests somewhere that never saw the initialize, which fails as 400 (no
    # Mcp-Session-Id) or 404 (unknown session).
    #
    # Set MCP_STATELESS=true to drop sessions entirely: every request becomes
    # self-contained, at the cost of server->client push (ctx.report_progress,
    # ctx.sample, ctx.elicit, resource-subscription notifications). search_people
    # doesn't use any of those, so stateless is a safe fallback if task restarts
    # or multi-task scaling become a problem.
    stateless = os.environ.get("MCP_STATELESS", "false").lower() == "true"

    asyncio.run(
        mcp.run_http_async(
            transport="streamable-http",
            host=host,
            port=port,
            path="/mcp",
            stateless_http=stateless,
            middleware=[
                Middleware(
                    RequireBearerOnMCP,
                    resource_metadata_url=resource_metadata_url,
                )
            ],
            # We sit behind an ALB that terminates TLS and forwards plain HTTP.
            # Without trusting X-Forwarded-Proto/-For, uvicorn thinks the scheme is
            # "http", so any redirect Starlette generates (notably the /mcp/ ->
            # /mcp trailing-slash 307) points at http://mcp.goprofiles.io. A client
            # that follows it gets downgraded to plain HTTP, bounces off the ALB's
            # 301 back to https, and loses its POST body on the way.
            uvicorn_config={
                "proxy_headers": True,
                "forwarded_allow_ips": "*",
            },
            show_banner=False,
        )
    )


if __name__ == "__main__":
    main()
