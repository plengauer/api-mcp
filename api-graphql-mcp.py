import os
import inspect

# --- Patches for graphql_mcp to handle huge schemas like GitHub ---
# Without these, GraphQLMCP.from_remote_url() never returns on the GitHub
# GraphQL schema:
#   1. Recursive Pydantic model generation explodes (4000+ types, each
#      embedding deep nested models).
#   2. GraphQL allows defaulted args before required args; Python signatures
#      don't ("non-default argument follows default argument").
#   3. GitHub has GraphQL field args named after Python keywords (e.g. `from`)
#      which inspect.Parameter rejects.
def _patch_graphql_mcp():
    import graphql_mcp.server as gs
    from graphql import (
        GraphQLNonNull, GraphQLList,
        GraphQLObjectType, GraphQLInputObjectType,
    )

    _orig_map = gs._map_graphql_type_to_python_type
    def patched_map(graphql_type, _cache=None):
        if isinstance(graphql_type, GraphQLNonNull):
            return patched_map(graphql_type.of_type, _cache)
        if isinstance(graphql_type, GraphQLList):
            return list[patched_map(graphql_type.of_type, _cache)]
        if isinstance(graphql_type, (GraphQLObjectType, GraphQLInputObjectType)):
            return dict
        return _orig_map(graphql_type, _cache)
    gs._map_graphql_type_to_python_type = patched_map

    _RealSignature = inspect.Signature
    _Param = inspect.Parameter
    class _PatchedInspect:
        def __getattr__(self, name): return getattr(inspect, name)
        Parameter = inspect.Parameter
        @staticmethod
        def Signature(parameters=None, *args, **kwargs):
            if parameters:
                params = list(parameters)
                seen_default = False
                illegal = False
                for p in params:
                    if p.kind == _Param.POSITIONAL_OR_KEYWORD:
                        if p.default is _Param.empty:
                            if seen_default:
                                illegal = True
                                break
                        else:
                            seen_default = True
                if illegal:
                    params = [
                        p.replace(kind=_Param.KEYWORD_ONLY)
                        if p.kind == _Param.POSITIONAL_OR_KEYWORD else p
                        for p in params
                    ]
                parameters = params
            return _RealSignature(parameters, *args, **kwargs)
    gs.inspect = _PatchedInspect()

    def _safe(orig):
        def w(*a, **kw):
            try:
                return orig(*a, **kw)
            except ValueError:
                return None
        return w
    for fname in (
        "_create_tool_function",
        "_create_remote_tool_function",
        "_create_recursive_tool_function",
        "_create_recursive_remote_tool_function",
    ):
        f = getattr(gs, fname, None)
        if f is not None:
            setattr(gs, fname, _safe(f))

    def _make_per_field_wrapper(orig):
        def w(server, schema, fields, *args, **kwargs):
            for field_name, field_def in fields.items():
                try:
                    orig(server, schema, {field_name: field_def}, *args, **kwargs)
                except Exception:
                    pass
        return w
    for fname in ("_add_tools_from_fields", "_add_tools_from_fields_remote"):
        f = getattr(gs, fname, None)
        if f is not None:
            setattr(gs, fname, _make_per_field_wrapper(f))

    def _safe_top(orig):
        def w(*a, **kw):
            try:
                return orig(*a, **kw)
            except Exception:
                return None
        return w
    for fname in ("_add_nested_tools_from_schema", "_add_nested_tools_from_schema_remote"):
        f = getattr(gs, fname, None)
        if f is not None:
            setattr(gs, fname, _safe_top(f))
_patch_graphql_mcp()
# --- End patch ---

from urllib.parse import parse_qs
from graphql_mcp.server import GraphQLMCP
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

class AuthFromQueryParam:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            qs = parse_qs(scope.get("query_string", b"").decode())
            token = qs.get("authorization", [""])[0]
            if token:
                headers = list(scope.get("headers", []))
                headers.append((b"authorization", token.encode()))
                scope["headers"] = headers
        await self.app(scope, receive, send)

mcp = GraphQLMCP.from_remote_url(
    url = os.environ["API_MCP_BASE_URL"],
    headers = {},
    forward_bearer_token = True,
    name = os.environ["API_MCP_SERVER_NAME"]
)

if __name__ == "__main__":
    app = mcp.http_app(middleware=[Middleware(AuthFromQueryParam)])
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
