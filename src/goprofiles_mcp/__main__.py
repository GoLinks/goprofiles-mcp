import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from starlette.middleware import Middleware  # noqa: E402

from goprofiles_mcp.server import RequireBearerOnMCP, mcp  # noqa: E402


def main() -> None:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    resource_metadata_url = os.environ.get(
        "MCP_RESOURCE_METADATA_URL",
        "https://mcp.goprofiles.io/.well-known/oauth-protected-resource/mcp",
    )
    # Stateful sessions live in the memory of one task, so they don't survive a
    # hosted client that spreads calls across workers, an ECS task restart, or a
    # second task behind the ALB — the follow-up lands somewhere that never saw
    # the initialize and gets 400 (no Mcp-Session-Id) or 404 (unknown session).
    # search_people is read-only and uses no progress/sampling/subscriptions, so
    # there is no reason to keep per-connection state. Escape hatch in case a
    # future tool needs server->client push.
    #
    # TEMPORARY (revert me): default flipped to stateful to confirm in prod that
    # ChatGPT's 502 is really the 400/404 above. Once confirmed, restore the
    # default to "true" — stateless is the correct mode for this server.
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
