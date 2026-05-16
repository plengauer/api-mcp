import os
import json

import pytest
from fastmcp import Client

TARGET_OWNER = "plengauer"
TARGET_REPO_FULL_NAME = "plengauer/api-mcp"
TARGET_REPO_NAME = "api-mcp"
MIN_EXPECTED_REPOSITORIES = 10


def _to_plain(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        text_items = value.get("content")
        if isinstance(text_items, list):
            for item in text_items:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    try:
                        return _to_plain(json.loads(item["text"]))
                    except json.JSONDecodeError:
                        continue
        return {key: _to_plain(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _looks_like_repository_list(value):
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, dict) for item in value) and any(
            "name" in item or "full_name" in item or "nameWithOwner" in item
            for item in value
        )
    return False


def _extract_repository_list(value):
    value = _to_plain(value)
    candidates = []

    def walk(node):
        if _looks_like_repository_list(node):
            candidates.append(node)
        if isinstance(node, list):
            if node and all(
                isinstance(item, dict) and isinstance(item.get("node"), dict)
                for item in node
            ):
                nested_nodes = [item["node"] for item in node]
                if _looks_like_repository_list(nested_nodes):
                    candidates.append(nested_nodes)
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            items = node.get("items")
            if _looks_like_repository_list(items):
                candidates.append(items)
            for nested in node.values():
                walk(nested)

    walk(value)
    if not candidates:
        return []
    return max(candidates, key=len)


def _is_api_mcp_repository(repository):
    full_name = repository.get("full_name") or repository.get("nameWithOwner")
    if isinstance(full_name, str):
        return full_name.lower() == TARGET_REPO_FULL_NAME
    owner = repository.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    name = repository.get("name")
    if isinstance(owner_login, str) and isinstance(name, str):
        return f"{owner_login}/{name}".lower() == TARGET_REPO_FULL_NAME
    return False


async def _call_rest_repository_tool(client, tool_names):
    tool_name = "searchrepos"
    if tool_name not in tool_names:
        raise AssertionError(f"Missing REST tool: {tool_name}")
    result = await client.call_tool_mcp(
        tool_name,
        {"q": f"user:{TARGET_OWNER}", "per_page": 30},
    )
    repositories = _extract_repository_list(result)
    if not repositories:
        raise AssertionError(f"REST tool '{tool_name}' returned no repository list")
    return tool_name, repositories


async def _call_graphql_repository_tool(client):
    tool_name = "viewer"
    for arguments in ({"repositories_first": 30}, {}):
        result = await client.call_tool_mcp(tool_name, arguments)
        repositories = _extract_repository_list(result)
        if repositories:
            return tool_name, repositories
    raise AssertionError(f"GraphQL tool '{tool_name}' returned no repository list")


async def _call_github_repository_listing_tool(client):
    tools = await client.list_tools()
    tool_names = {tool.name for tool in tools}
    if "viewer" in tool_names:
        return await _call_graphql_repository_tool(client)
    return await _call_rest_repository_tool(client, tool_names)


@pytest.mark.asyncio
async def test_mcp_lists_github_repositories():
    async with Client(os.environ["MCP_URL"]) as client:
        tool_name, repositories = await _call_github_repository_listing_tool(client)

    assert isinstance(repositories, list), f"{tool_name} should return a repository list"
    assert len(repositories) >= MIN_EXPECTED_REPOSITORIES, (
        f"{tool_name} returned only {len(repositories)} repositories"
    )
    assert any(_is_api_mcp_repository(repository) for repository in repositories), (
        f"{tool_name} response did not include {TARGET_REPO_FULL_NAME}"
    )
