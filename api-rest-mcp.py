import os
from contextvars import ContextVar
from urllib.parse import parse_qs
import httpx2
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

class DynamicAuth(httpx2.Auth):
    def auth_flow(self, request):
        base = httpx2.URL(os.environ["API_MCP_BASE_URL"])
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

def method_annotations(method):
    read_only = method in READ_METHODS
    return ToolAnnotations(
        readOnlyHint = read_only,
        destructiveHint = not read_only,
        idempotentHint = method in IDEMPOTENT_METHODS,
        openWorldHint = True,
    )

def annotate_read_or_write(route, component):
    if not isinstance(component, OpenAPITool):
        return
    component.annotations = method_annotations(route.method.upper())

# Shared with the manual, method-named tools below so raw fallback calls go
# through the same base URL resolution and DynamicAuth authorization as
# every generated tool.
raw_client = httpx2.AsyncClient(
    base_url = os.environ["API_MCP_BASE_URL"],
    auth = DynamicAuth(),
    follow_redirects = True,
)

mcp = FastMCP.from_openapi(
    openapi_spec = fix_spec(httpx2.get(os.environ["API_MCP_OPENAPI_SPEC_URL"], follow_redirects=True).raise_for_status().json()),
    client = raw_client,
    name = os.environ["API_MCP_SERVER_NAME"],
    route_maps = route_maps,
    mcp_component_fn = annotate_read_or_write,
)

# Manual, method-named tools that can call ANY path on this API, in addition
# to the tools generated above from the OpenAPI spec. These exist purely as a
# fallback for endpoints the spec omits or describes incorrectly: a generated
# tool is always better typed (named for the operation, with typed/validated
# parameters instead of a raw path-and-query string and a raw body), so every
# one of these tools is documented to be a last resort. They reuse raw_client
# so the request still gets the right base URL joining and Authorization
# header via DynamicAuth.
FALLBACK_NOTE = (
    "Only use this if there is no better, more specific tool already "
    "available for this operation - prefer a generated tool whenever one "
    "exists for the endpoint you need, and fall back to this raw call only "
    "when none of them fit."
)

def _response_result(response: httpx2.Response) -> dict:
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response.text,
    }

@mcp.tool(
    description=f"Make a raw HTTP GET request to this API. {FALLBACK_NOTE}",
    tags={"read"},
    annotations=method_annotations("GET"),
)
async def HTTP_GET(path_and_query: str):
    response = await raw_client.get(path_and_query)
    return _response_result(response)

@mcp.tool(
    description=f"Make a raw HTTP POST request to this API. {FALLBACK_NOTE}",
    tags={"write"},
    annotations=method_annotations("POST"),
)
async def HTTP_POST(path_and_query: str, body: str, body_content_type: str = "application/json"):
    response = await raw_client.post(path_and_query, content=body, headers={"Content-Type": body_content_type})
    return _response_result(response)

@mcp.tool(
    description=f"Make a raw HTTP PUT request to this API. {FALLBACK_NOTE}",
    tags={"write"},
    annotations=method_annotations("PUT"),
)
async def HTTP_PUT(path_and_query: str, body: str, body_content_type: str = "application/json"):
    response = await raw_client.put(path_and_query, content=body, headers={"Content-Type": body_content_type})
    return _response_result(response)

@mcp.tool(
    description=f"Make a raw HTTP PATCH request to this API. {FALLBACK_NOTE}",
    tags={"write"},
    annotations=method_annotations("PATCH"),
)
async def HTTP_PATCH(path_and_query: str, body: str, body_content_type: str = "application/json"):
    response = await raw_client.patch(path_and_query, content=body, headers={"Content-Type": body_content_type})
    return _response_result(response)

@mcp.tool(
    description=f"Make a raw HTTP DELETE request to this API. {FALLBACK_NOTE}",
    tags={"write"},
    annotations=method_annotations("DELETE"),
)
async def HTTP_DELETE(path_and_query: str):
    response = await raw_client.delete(path_and_query)
    return _response_result(response)

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
