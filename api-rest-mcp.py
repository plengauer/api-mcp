import builtins
import functools
import io
import os
import sys
from contextvars import ContextVar
from urllib.parse import parse_qs
import httpx
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

def _protect_stdio_transport():
    """When running over stdio transport, stdout *is* the MCP JSON-RPC wire:
    fastmcp/mcp build the protocol stream directly from sys.stdout.buffer
    (see mcp.server.stdio.stdio_server). Any dependency that writes to
    stdout by ANY means -- print(), a stray logging handler, output from a
    C extension, or even a subprocess that inherits fd 1 -- injects plain
    text into the JSON-RPC stream. That corrupts a response line so the
    client can't parse it and hangs until timeout, even though the tool
    call itself succeeded.

    A bare print() patch only catches Python's print() builtin, not every
    other way a write can reach fd 1. Instead we replicate, at the OS file
    descriptor level, the same protection the upstream `mcp` package added
    in v2.0 (see mcp-server issue #1933 / PR #3117) -- unavailable here
    since fastmcp pins mcp<2.0:
      1. Duplicate the real stdout fd (1, the actual wire) onto a private
         descriptor that only this function holds a reference to.
      2. Point fd 1 itself at fd 2 (stderr)'s target, so every future write
         to "stdout" -- by any mechanism, in this process or a child one --
         lands alongside stderr/docker logs instead of on the wire.
      3. Hand the private descriptor to mcp.server.stdio.stdio_server()
         explicitly, so the actual JSON-RPC traffic is unaffected.
    """
    if os.environ.get("API_MCP_MODE", "http") != "stdio":
        return

    # Always keep print() pointed at stderr, regardless of whether the
    # fd-level trick below can engage (e.g. under a test harness that
    # replaces sys.stdout with a non-fd-backed object). This is a fallback
    # safety net; the fd-level trick below is the primary protection.
    builtins.print = functools.partial(print, file=sys.stderr)

    try:
        if sys.stdout.buffer.fileno() != 1:
            return
    except (AttributeError, OSError, ValueError):
        return

    import anyio
    import mcp.server.stdio as mcp_stdio

    private_fd = os.dup(1)
    os.dup2(2, 1)
    wire_stream = anyio.wrap_file(io.TextIOWrapper(os.fdopen(private_fd, "wb"), encoding="utf-8"))

    original_stdio_server = mcp_stdio.stdio_server

    def _stdio_server_with_protected_wire(stdin=None, stdout=None):
        return original_stdio_server(stdin=stdin, stdout=stdout if stdout is not None else wire_stream)

    mcp_stdio.stdio_server = _stdio_server_with_protected_wire
    try:
        import fastmcp.server.mixins.transport as fastmcp_transport
        fastmcp_transport.stdio_server = _stdio_server_with_protected_wire
    except ImportError:
        pass


_protect_stdio_transport()

authorization_var: ContextVar[str] = ContextVar("authorization", default="")

class AuthFromQueryParam:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            qs = parse_qs(scope.get("query_string", b"").decode())
            token = qs.get("authorization", [""])[0]
            authorization_var.set(token)
            if token:
                headers = list(scope.get("headers", []))
                headers.append((b"authorization", token.encode()))
                scope["headers"] = headers
        await self.app(scope, receive, send)

class DynamicAuth(httpx.Auth):
    def auth_flow(self, request):
        base = httpx.URL(os.environ["API_MCP_BASE_URL"])
        if request.url.scheme == base.scheme and request.url.host == base.host and request.url.port == base.port:
            token = authorization_var.get() or os.environ.get("HTTP_AUTHORIZATION", "")
            if token:
                request.headers["Authorization"] = token
        yield request

def fix_spec(obj):
    if isinstance(obj, dict):
        return {
            k: fix_spec(v)
            for k, v in obj.items()
            if not (k == "enum" and v == [])
        }
    elif isinstance(obj, list):
        return [fix_spec(item) for item in obj]
    return obj

mcp = FastMCP.from_openapi(
    openapi_spec = fix_spec(httpx.get(os.environ["API_MCP_OPENAPI_SPEC_URL"], follow_redirects=True).raise_for_status().json()),
    client = httpx.AsyncClient(
        base_url = os.environ["API_MCP_BASE_URL"],
        auth = DynamicAuth(),
        follow_redirects = True
    ),
    name = os.environ["API_MCP_SERVER_NAME"]
)

if __name__ == "__main__":
    mode = os.environ.get("API_MCP_MODE", "http")
    if mode == "stdio":
        mcp.run()
    else:
        app = mcp.http_app(middleware=[Middleware(AuthFromQueryParam)])
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8080)
