"""Template for a GoProfiles MCP tool module.

Copy this file to `src/goprofiles_mcp/tools/<name>.py`, rename the placeholders,
and register the tool function in `goprofiles_mcp.server`:

    from goprofiles_mcp.tools.<name> import search_examples

    mcp.add_tool(
        FunctionTool.from_function(
            search_examples,
            title="Search examples",
            annotations=ToolAnnotations(
                title="Search examples",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
    )

Conventions carried over from golinks-mcp:
  * One module per resource (or tool group); several tool functions per module is fine.
  * Tool functions are `async def`, take `ctx: Context | None = None` last, and
    return a human-readable `str` — not JSON.
  * Every parameter is `Annotated[..., Field(description=...)]`; the description
    is what the model sees, so write it for the model.
  * The docstring is the tool description. Say what it does, when to use it,
    whether it's read-only, and which OAuth scope it needs.
  * Parse responses with pydantic models that default every field, so an API
    field going missing degrades instead of raising.
"""

from typing import Annotated, Literal

import httpx
from fastmcp import Context
from pydantic import BaseModel, Field

from goprofiles_mcp.client import (
    SortOrder,
    external_params,
    format_timestamp,
    get_authorization_header,
    http_client,
    raise_for_status,
)

# ---------------------------------------------------------------------------
# Filter/sort literals
# ---------------------------------------------------------------------------
# Narrow enums the model must choose from. Keep the values identical to what the
# API accepts so they can be passed straight through as query params.

ExampleStatus = Literal["active", "inactive"]

ExampleSort = Literal["name", "created_at", "updated_at"]

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
# Mirror the API response shape. Give every field a default — the API adding or
# dropping a field should never turn into a validation error mid-tool-call.


class ExampleResult(BaseModel):
    id: int = 0
    name: str = ""
    status: str | None = None
    created_at: int | None = None


class ExamplePaginationMetadata(BaseModel):
    limit: int = 0
    offset: int = 0
    total_results: int = 0
    count: int = 0


class ExampleListResponse(BaseModel):
    metadata: ExamplePaginationMetadata = ExamplePaginationMetadata()
    results: list[ExampleResult] = []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
# Render one record as aligned `Label: value` lines. Prefer stable labels and
# explicit "Unknown" over omitting fields, so output is predictable to parse.


def _format_example(e: ExampleResult) -> str:
    lines = [
        f"ID:      {e.id}",
        f"Name:    {e.name or 'Unknown'}",
        f"Status:  {e.status or 'Unknown'}",
        f"Created: {format_timestamp(e.created_at)}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search_examples(
    search: Annotated[
        str | None,
        Field(
            description="Search term matched against the example name (substring match)."
        ),
    ] = None,
    status: Annotated[
        ExampleStatus | None,
        Field(description="Restrict results to 'active' or 'inactive' examples."),
    ] = None,
    limit: Annotated[
        int, Field(description="Number of examples to return (1–100).", ge=1, le=100)
    ] = 20,
    offset: Annotated[int, Field(description="Pagination offset (0-based).", ge=0)] = 0,
    sort: Annotated[
        ExampleSort | None,
        Field(description="Sort order: 'name', 'created_at', or 'updated_at'."),
    ] = None,
    order: Annotated[
        SortOrder | None, Field(description="Sort direction: 'asc' or 'desc'.")
    ] = None,
    ctx: Context | None = None,
) -> str:
    """One-line summary of what this tool returns, from the user's GoProfiles
    workspace (https://www.goprofiles.io).

    Follow with when the model should reach for this tool versus a related one,
    and note whether it is read-only.

    Requires <scope> scope.
    """
    if ctx is None:
        raise PermissionError("Missing request context.")
    authorization = get_authorization_header(ctx)

    # Only send params the caller actually set — let the API apply its defaults.
    raw_params: dict = {"limit": limit, "offset": offset}
    if search:
        raw_params["search"] = search
    if status is not None:
        raw_params["status"] = status
    if sort is not None:
        raw_params["sort"] = sort
    if order is not None:
        raw_params["order"] = order

    params = external_params(raw_params, tool="search_examples")

    try:
        response = await http_client.get(
            "/examples",
            params=params,
            headers={"Authorization": authorization},
        )
    except httpx.TimeoutException:
        raise TimeoutError("Request to GoProfiles API timed out.")
    except httpx.ConnectError:
        raise ConnectionError("Failed to connect to GoProfiles API.")

    raise_for_status(response, "/examples")

    data = ExampleListResponse.model_validate(response.json())

    # An empty result is a normal outcome, not an error — say so plainly.
    if not data.results:
        return "No examples found."

    m = data.metadata
    header = f"Examples ({m.count} of {m.total_results} total, offset {m.offset}):\n"
    entries = [f"[{i}]\n{_format_example(e)}" for i, e in enumerate(data.results, 1)]
    return header + "\n\n".join(entries)
