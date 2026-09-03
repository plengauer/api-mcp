import asyncio
import importlib.util
import io
import os
from contextlib import redirect_stdout

import pytest

pytest.importorskip("graphql_mcp")
pytest.importorskip("graphql")


def _load_api_graphql_mcp():
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api-graphql-mcp.py",
    )
    spec = importlib.util.spec_from_file_location("api_graphql_mcp", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_graphql_mcp_resolver_errors_do_not_leak_to_stdout():
    """graphql_mcp.server logs resolver/query errors via bare print(), which by
    default writes to stdout. Since stdio mode uses stdout as the MCP JSON-RPC
    transport, any such print() would corrupt the protocol stream and cause
    every subsequent tool call to hang. Importing api-graphql-mcp.py must patch
    graphql_mcp's print() to write to stderr instead, so a failing tool call
    never emits anything on stdout.
    """
    mod = _load_api_graphql_mcp()

    from graphql import build_schema

    schema = build_schema(
        """
        type Query {
          boom: String
        }
        """
    )

    async def resolve_boom(*_args, **_kwargs):
        raise ValueError("kaboom")

    schema.query_type.fields["boom"].resolve = resolve_boom

    server = mod.GraphQLMCP(schema=schema, name="test")
    tool = await server.get_tool("boom")

    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        with pytest.raises(Exception):
            await tool.fn()

    assert captured_stdout.getvalue() == ""
