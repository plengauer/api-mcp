import os

import pytest
from fastmcp import Client


def _extract_repository_list(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()

    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            if any(
                "name" in item or "full_name" in item or "nameWithOwner" in item
                for item in value
            ):
                return value
        for item in value:
            repositories = _extract_repository_list(item)
            if repositories:
                return repositories
        return []

    if isinstance(value, dict):
        for nested_value in value.values():
            repositories = _extract_repository_list(nested_value)
            if repositories:
                return repositories

    return []


async def _call_github_repository_listing_tool(client):
    tools = await client.list_tools()
    tool_names = {tool.name for tool in tools}

    if "repos_list_for_authenticated_user" in tool_names:
        return await client.call_tool(
            "repos_list_for_authenticated_user",
            {"per_page": 5},
        )

    graphql_tool_names = [
        tool_name
        for tool_name in tool_names
        if "viewer" in tool_name and ("repo" in tool_name or "repositories" in tool_name)
    ]
    for tool_name in sorted(graphql_tool_names):
        for arguments in (
            {"first": 5},
            {"last": 5},
            {"repositories_first": 5},
            {"repositories_last": 5},
            {},
        ):
            try:
                return await client.call_tool(tool_name, arguments)
            except Exception:
                continue

    raise AssertionError("No GitHub repository-listing MCP tool was callable")


@pytest.mark.asyncio
async def test_mcp_lists_github_repositories():
    async with Client(os.environ["MCP_URL"]) as client:
        result = await _call_github_repository_listing_tool(client)

    repositories = _extract_repository_list(result)
    assert repositories
