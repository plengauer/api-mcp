import os
import pytest
from fastmcp import Client


@pytest.mark.asyncio
async def test_mcp_lists_tools():
    async with Client(os.environ["MCP_URL"]) as client:
        tools = await client.list_tools()
    assert len(tools) >= 50
    for tool in tools:
        assert len(tool.name) <= 64


def _hint(annotations, name):
    """Read an annotation hint regardless of MCP SDK field-naming convention."""
    if annotations is None:
        return None
    dumped = annotations.model_dump(by_alias=True)
    camel = name[0] + name.title().replace("_", "")[1:]
    return dumped.get(camel, dumped.get(name))


@pytest.mark.asyncio
async def test_mcp_tools_are_annotated_read_or_write():
    """Every generated tool must declare whether it reads or writes.

    The read/write split is driven by the HTTP method of the underlying route,
    and it has to reach clients as standard MCP tool annotations. FastMCP tags
    alone are not enough: they are FastMCP-specific metadata that surfaces only
    under _meta.fastmcp.tags and is invisible to MCP clients.
    """
    if os.environ.get("API_MCP_TYPE", "rest") != "rest":
        pytest.skip("read/write classification is HTTP-method based, REST only")

    async with Client(os.environ["MCP_URL"]) as client:
        tools = await client.list_tools()

    assert tools
    unannotated = [tool.name for tool in tools if _hint(tool.annotations, "read_only_hint") is None]
    assert not unannotated, f"tools without a readOnlyHint annotation: {unannotated[:10]}"

    read_only = [tool.name for tool in tools if _hint(tool.annotations, "read_only_hint")]
    writing = [tool.name for tool in tools if not _hint(tool.annotations, "read_only_hint")]
    assert read_only, "no tool is annotated as read-only"
    assert writing, "no tool is annotated as writing"

    for tool in tools:
        if _hint(tool.annotations, "read_only_hint"):
            assert not _hint(tool.annotations, "destructive_hint"), (
                f"{tool.name} is read-only but annotated destructive"
            )
