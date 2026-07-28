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

    asyncio.run(
        mcp.run_http_async(
            transport="streamable-http",
            host=host,
            port=port,
            path="/mcp",
            middleware=[
                Middleware(
                    RequireBearerOnMCP,
                    resource_metadata_url=resource_metadata_url,
                )
            ],
            show_banner=False,
        )
    )


if __name__ == "__main__":
    main()
