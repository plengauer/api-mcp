import os
import pytest
from fastmcp import Client


@pytest.mark.asyncio
async def test_mcp_lists_tools():
    async with Client(os.environ["MCP_URL"]) as client:
        tools = await client.list_tools()
    assert len(tools) > 0
