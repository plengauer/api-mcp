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
    tools_by_name = {tool.name: tool for tool in tools}

    candidates = [
        ("repos_list_for_authenticated_user", {"per_page": 5}),
        ("reposlist_for_authenticated_user", {"per_page": 5}),
        ("viewer", {"repositories_first": 5}),
        ("viewer", {"repositories_last": 5}),
    ]

    for tool in tools:
        properties = set((tool.inputSchema or {}).get("properties", {}).keys())
        if {"repositories_first", "repositories_last"} & properties:
            if "repositories_first" in properties:
                candidates.append((tool.name, {"repositories_first": 5}))
            if "repositories_last" in properties:
                candidates.append((tool.name, {"repositories_last": 5}))
        if {
            "visibility",
            "affiliation",
            "type",
            "sort",
            "direction",
            "per_page",
        }.issubset(properties):
            candidates.append((tool.name, {"per_page": 5}))

    attempted = []
    for tool_name, arguments in candidates:
        if tool_name not in tools_by_name:
            continue
        attempted.append(tool_name)
        try:
            result = await client.call_tool(tool_name, arguments)
        except Exception:
            continue
        if _extract_repository_list(result):
            return result

    raise AssertionError(
        "No GitHub repository-listing MCP tool returned repositories. "
        f"Tried: {sorted(set(attempted))}"
    )


@pytest.mark.asyncio
async def test_mcp_lists_github_repositories():
    async with Client(os.environ["MCP_URL"]) as client:
        result = await _call_github_repository_listing_tool(client)

    repositories = _extract_repository_list(result)
    assert repositories
