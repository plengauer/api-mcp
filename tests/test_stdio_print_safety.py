import builtins
import importlib.util
import io
import os
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module(filename, monkeypatch, env=None):
    """Load one of the api-*-mcp.py entrypoint scripts as a fresh module."""
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    module_path = os.path.join(REPO_ROOT, filename)
    spec = importlib.util.spec_from_file_location(
        os.path.splitext(filename)[0].replace("-", "_"), module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_graphql_mcp_resolver_errors_do_not_leak_to_stdout(monkeypatch):
    """graphql_mcp.server logs resolver/query errors via bare print(), which by
    default writes to stdout. Since stdio mode uses stdout as the MCP JSON-RPC
    transport, any such print() would corrupt the protocol stream and cause
    every subsequent tool call to hang. Importing api-graphql-mcp.py in stdio
    mode must redirect print() to stderr, so a failing tool call never emits
    anything on stdout.
    """
    pytest.importorskip("graphql_mcp")
    pytest.importorskip("graphql")

    monkeypatch.setattr(builtins, "print", builtins.print)
    mod = _load_module(
        "api-graphql-mcp.py", monkeypatch, env={"API_MCP_MODE": "stdio"}
    )

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


def test_print_is_redirected_to_stderr_in_stdio_mode_for_rest(monkeypatch, capsys):
    """Any dependency (present or future) that calls a bare print() while the
    REST server runs in stdio mode must not corrupt the MCP JSON-RPC stream on
    stdout. api-rest-mcp.py must redirect print() to stderr for the lifetime
    of the process when API_MCP_MODE=stdio.
    """
    pytest.importorskip("fastmcp")
    original_print = builtins.print
    monkeypatch.setattr(builtins, "print", original_print)

    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **kw: type(
            "Resp",
            (),
            {
                "raise_for_status": lambda self: self,
                "json": lambda self: {
                    "openapi": "3.0.0",
                    "info": {"title": "t", "version": "1"},
                    "paths": {},
                },
            },
        )(),
    )

    _load_module(
        "api-rest-mcp.py",
        monkeypatch,
        env={
            "API_MCP_MODE": "stdio",
            "API_MCP_OPENAPI_SPEC_URL": "http://example.invalid/spec",
            "API_MCP_BASE_URL": "http://example.invalid",
            "API_MCP_SERVER_NAME": "test",
        },
    )

    print("this must not reach stdout")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "this must not reach stdout" in captured.err


def test_print_is_unaffected_in_http_mode_for_rest(monkeypatch, capsys):
    """Outside of stdio mode, stdout is not the MCP transport, so print()
    should behave normally (not be redirected)."""
    pytest.importorskip("fastmcp")

    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **kw: type(
            "Resp",
            (),
            {
                "raise_for_status": lambda self: self,
                "json": lambda self: {
                    "openapi": "3.0.0",
                    "info": {"title": "t", "version": "1"},
                    "paths": {},
                },
            },
        )(),
    )

    _load_module(
        "api-rest-mcp.py",
        monkeypatch,
        env={
            "API_MCP_MODE": "http",
            "API_MCP_OPENAPI_SPEC_URL": "http://example.invalid/spec",
            "API_MCP_BASE_URL": "http://example.invalid",
            "API_MCP_SERVER_NAME": "test",
        },
    )

    print("this should reach stdout")
    captured = capsys.readouterr()
    assert "this should reach stdout" in captured.out
