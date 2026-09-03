import builtins
import importlib.util
import io
import os
import subprocess
import sys
import textwrap
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
async def test_graphql_mcp_resolver_errors_do_not_leak_to_stdout(monkeypatch, capsys):
    """graphql_mcp.server logs resolver/query errors via bare print(), which by
    default writes to stdout. Since stdio mode uses stdout as the MCP JSON-RPC
    transport, any such print() would corrupt the protocol stream and cause
    every subsequent tool call to hang. Importing api-graphql-mcp.py in stdio
    mode must redirect print() to stderr, so a failing tool call never emits
    anything on stdout.

    Runs under the `capsys` fixture (sys.stdout isn't backed by real fd 1
    there) so only the belt-and-suspenders builtins.print patch engages, not
    the OS file-descriptor trick, which must never run against the test
    process's own stdio (see the subprocess-based REST tests below for why).
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


_MOCK_HTTPX_GET = """
import httpx as _httpx
_httpx.get = lambda *a, **kw: type(
    "Resp", (), {
        "raise_for_status": lambda self: self,
        "json": lambda self: {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {},
        },
    },
)()
"""


def _run_rest_module_in_subprocess(tmp_path, api_mcp_mode):
    """Load the real api-rest-mcp.py in a fresh subprocess and have it emit
    stray writes to stdout via every mechanism a dependency might use, then
    report which of the parent's real stdout/stderr streams they landed on.

    This deliberately runs in a subprocess rather than in-process: the
    stdio-mode protection duplicates and reassigns file descriptor 1 for
    the rest of the process's life, which would corrupt pytest's own
    stdout/stderr capturing for this and any later test if done in the
    pytest worker itself.
    """
    script = tmp_path / "run_rest.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            import sys
            import importlib.util

            os.environ["API_MCP_MODE"] = {api_mcp_mode!r}
            os.environ["API_MCP_OPENAPI_SPEC_URL"] = "http://example.invalid/spec"
            os.environ["API_MCP_BASE_URL"] = "http://example.invalid"
            os.environ["API_MCP_SERVER_NAME"] = "test"

            {textwrap.indent(_MOCK_HTTPX_GET, "            ").strip()}

            module_path = {os.path.join(REPO_ROOT, "api-rest-mcp.py")!r}
            spec = importlib.util.spec_from_file_location("api_rest_mcp", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            print("print-leak")
            os.write(1, b"raw-fd1-leak\\n")
            sys.stdout.write("sys-stdout-write-leak\\n")
            sys.stdout.flush()
            """
        )
    )
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, timeout=30
    )


def test_stdio_mode_protects_stdout_from_any_stray_write(tmp_path):
    """In stdio mode, stdout is the MCP JSON-RPC wire. A stray write to
    stdout via print(), a raw os.write(1, ...), or a direct
    sys.stdout.write() -- not just print() -- must not reach the real
    stdout stream, or it would corrupt a JSON-RPC response line and hang
    the client, even for an otherwise-successful tool call.
    """
    pytest.importorskip("fastmcp")

    result = _run_rest_module_in_subprocess(tmp_path, "stdio")
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b""

    stderr = result.stderr.decode()
    assert "print-leak" in stderr
    assert "raw-fd1-leak" in stderr
    assert "sys-stdout-write-leak" in stderr


def test_http_mode_does_not_touch_stdout(tmp_path):
    """Outside of stdio mode, stdout isn't the MCP transport, so writes
    should behave completely normally (no redirection)."""
    pytest.importorskip("fastmcp")

    result = _run_rest_module_in_subprocess(tmp_path, "http")
    assert result.returncode == 0, result.stderr.decode()

    stdout = result.stdout.decode()
    assert "print-leak" in stdout
    assert "raw-fd1-leak" in stdout
    assert "sys-stdout-write-leak" in stdout
