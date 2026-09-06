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
async def test_mcp_tools_are_classified_read_or_write():
    """No tool may land in a host's "other tools" bucket.

    Hosts group connector tools on two annotations and treat them as mutually
    exclusive flags: readOnlyHint=true is a read, destructiveHint=true is a
    write/delete, and a tool that asserts NEITHER is left unclassified - Claude
    files those under "other tools". Setting readOnlyHint alone is therefore
    not enough: a mutating tool with destructiveHint=false asserts nothing and
    disappears into that bucket.
    """
    async with Client(os.environ["MCP_URL"]) as client:
        tools = await client.list_tools()

    assert tools

    reads, writes, unclassified = [], [], []
    for tool in tools:
        read_only = _hint(tool.annotations, "read_only_hint")
        destructive = _hint(tool.annotations, "destructive_hint")
        if read_only is True and destructive is False:
            reads.append(tool.name)
        elif destructive is True and read_only is False:
            writes.append(tool.name)
        else:
            unclassified.append(tool.name)

    assert not unclassified, (
        f"{len(unclassified)} tool(s) assert neither readOnlyHint nor "
        f"destructiveHint and will show up as \"other tools\": "
        f"{unclassified[:10]}"
    )
    assert reads, "no tool is classified as a read"
    assert writes, "no tool is classified as a write"
