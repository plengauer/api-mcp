import builtins
import functools
import os
import sys
from contextvars import ContextVar
from urllib.parse import parse_qs
import httpx
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

# In stdio mode, stdout *is* the MCP JSON-RPC transport (see mcp.server.stdio),
# so any dependency that writes diagnostics via a bare print() call (which
# defaults to stdout) corrupts the protocol stream and makes every subsequent
# tool call hang until the client times out — even for otherwise-successful
# responses. Force print() to go to stderr by default so no library can leak
# output onto the transport channel.
if os.environ.get("API_MCP_MODE", "http") == "stdio":
    builtins.print = functools.partial(print, file=sys.stderr)

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
