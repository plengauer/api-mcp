import os
import pytest
from fastmcp import Client
from graphql_mcp.server import GraphQLMCP


@pytest.mark.asyncio
async def test_github_graphql_mcp_lists_tools():
    """Test that the GitHub GraphQL MCP server starts and lists tools."""
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        pytest.skip("GITHUB_TOKEN not set")

    mcp = GraphQLMCP.from_remote_url(
        url="https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {github_token}"},
        name="GitHub GraphQL Test"
    )

    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert len(tools) > 0, "Expected at least one tool from the GitHub GraphQL API"
