import os
from contextvars import ContextVar
from urllib.parse import parse_qs
import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import RouteMap, MCPType, OpenAPITool
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

authorization_var: ContextVar[str] = ContextVar("authorization", default="")

class AuthFromHeaderOrQueryParam:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            token = ""
            for name, value in scope.get("headers", []):
                if name.lower() == b"authorization":
                    token = value.decode()
                    break
            if not token:
                qs = parse_qs(scope.get("query_string", b"").decode())
                token = qs.get("authorization", [""])[0]
                if token:
                    headers = list(scope.get("headers", []))
                    headers.append((b"authorization", token.encode()))
                    scope["headers"] = headers
            authorization_var.set(token)
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
            k: (
                v.replace("/", "_") if k == "operationId" and isinstance(v, str)
                else fix_spec(v)
            )
            for k, v in obj.items()
            if not (k == "enum" and v == [])
        }
    elif isinstance(obj, list):
        return [fix_spec(item) for item in obj]
    return obj

# Classify every generated tool as "read" or "write" based on the HTTP method
# of the underlying route, so MCP clients can distinguish read-only calls from
# ones with side effects. This keeps every route a plain TOOL - no existing
# tool is renamed, removed, or turned into a resource.
#
# The classification is published two ways:
#   * as MCP tool annotations, which is the standard, client-visible mechanism,
#     and
#   * as FastMCP tags, for clients that support tag-based tool filtering.
# Tags alone are FastMCP-specific metadata (they only show up under
# _meta.fastmcp.tags) and are ignored by MCP clients, so without the
# annotations the read/write split is invisible in tools/list.
#
# readOnlyHint and destructiveHint are what hosts actually group on, and they
# treat the two as mutually exclusive flags: readOnlyHint=true means "read",
# destructiveHint=true means "write/delete", and a tool that asserts NEITHER is
# left unclassified - Claude files those under "other tools". So every write
# method must set destructiveHint=true, including POST and PATCH: HTTP's
# create-vs-replace distinction is finer than the one flag can carry, and
# leaving it false on a mutating call reads as "no assertion", not as "safe".
# idempotentHint still carries the finer HTTP semantics for clients that want
# it.
READ_METHODS = ["GET", "HEAD", "OPTIONS", "TRACE"]
WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"]
# Repeating the call has the same effect as making it once.
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"}

route_maps = [
    RouteMap(methods=READ_METHODS, mcp_type=MCPType.TOOL, mcp_tags={"read"}),
    RouteMap(methods=WRITE_METHODS, mcp_type=MCPType.TOOL, mcp_tags={"write"}),
]

def annotate_read_or_write(route, component):
    if not isinstance(component, OpenAPITool):
        return
    method = route.method.upper()
    read_only = method in READ_METHODS
    component.annotations = ToolAnnotations(
        readOnlyHint = read_only,
        destructiveHint = not read_only,
        idempotentHint = method in IDEMPOTENT_METHODS,
        openWorldHint = True,
    )

mcp = FastMCP.from_openapi(
    openapi_spec = fix_spec(httpx.get(os.environ["API_MCP_OPENAPI_SPEC_URL"], follow_redirects=True).raise_for_status().json()),
    client = httpx.AsyncClient(
        base_url = os.environ["API_MCP_BASE_URL"],
        auth = DynamicAuth(),
        follow_redirects = True
    ),
    name = os.environ["API_MCP_SERVER_NAME"],
    route_maps = route_maps,
    mcp_component_fn = annotate_read_or_write,
)

if __name__ == "__main__":
    mode = os.environ.get("API_MCP_MODE", "http")
    if mode == "stdio":
        mcp.run()
    else:
        app = mcp.http_app(
            middleware=[Middleware(AuthFromHeaderOrQueryParam)],
            stateless_http=True,
        )
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8080)
