import os
import pytest
import httpx
from fastmcp import FastMCP, Client


def fix_spec(obj):
    # Duplicated here intentionally: importing api-rest-mcp directly would
    # trigger env-var checks and the OpenAPI spec fetch at import time.
    # See api-rest-mcp.py for the canonical docstring.
    if isinstance(obj, dict):
        return {
            k: fix_spec(v)
            for k, v in obj.items()
            if not (k == "enum" and v == [])
        }
    elif isinstance(obj, list):
        return [fix_spec(item) for item in obj]
    return obj


@pytest.mark.asyncio
async def test_github_rest_mcp_lists_tools():
    """Test that the GitHub REST MCP server starts and lists tools."""
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        pytest.skip("GITHUB_TOKEN not set")

    mcp = FastMCP.from_openapi(
        openapi_spec=fix_spec(
            httpx.get(
                "https://raw.githubusercontent.com/github/rest-api-description"
                "/main/descriptions/api.github.com/api.github.com.json",
                follow_redirects=True
            ).raise_for_status().json()
        ),
        client=httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {github_token}"}
        ),
        name="GitHub REST Test"
    )

    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert len(tools) > 0, "Expected at least one tool from the GitHub REST API"
