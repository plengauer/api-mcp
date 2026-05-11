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
    candidate_scores = {}
    for tool in tools:
        score = 0
        text = " ".join(
            [
                tool.name.lower(),
                (tool.title or "").lower(),
                (tool.description or "").lower(),
            ]
        )
        if "reposlist_for_authenticated_user" in text:
            score += 100
        if "repos_list_for_authenticated_user" in text:
            score += 100
        if "authenticated user" in text and "repositor" in text:
            score += 20
        if "viewer" in text and ("repo" in text or "repositor" in text):
            score += 10
        if score > 0:
            candidate_scores[tool.name] = max(candidate_scores.get(tool.name, 0), score)

    for tool_name, _ in sorted(
        candidate_scores.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        for arguments in (
            {"first": 5},
            {"last": 5},
            {"repositories_first": 5},
            {"repositories_last": 5},
            {"per_page": 5},
            {},
        ):
            try:
                result = await client.call_tool(tool_name, arguments)
            except Exception:
                continue
            if _extract_repository_list(result):
                return result

    raise AssertionError(
        "No GitHub repository-listing MCP tool returned repositories. "
        f"Candidates: {sorted(candidate_scores)}"
    )


@pytest.mark.asyncio
async def test_mcp_lists_github_repositories():
    async with Client(os.environ["MCP_URL"]) as client:
        result = await _call_github_repository_listing_tool(client)

    repositories = _extract_repository_list(result)
    assert repositories
