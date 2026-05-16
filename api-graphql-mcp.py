import asyncio
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
    from graphql.pyutils import Undefined

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

    def _build_selection_set_skip_required_args(
        graphql_type,
        max_depth=5,
        depth=0,
        _seen_types=frozenset(),
    ):
        """Build a GraphQL selection set while skipping fields that break GitHub execution."""
        if depth >= max_depth:
            return ""

        named_type = gs.get_named_type(graphql_type)
        if gs.is_leaf_type(named_type):
            return ""

        type_name = named_type.name
        is_cycle = type_name in _seen_types
        seen_with_current = _seen_types | {type_name}

        selections = []
        if hasattr(named_type, "fields"):
            for field_name, field_def in named_type.fields.items():
                # GitHub returns NOT_FOUND for classic projects; omit to keep query tools usable.
                if field_name == "projects":
                    continue
                field_args = getattr(field_def, "args", {}) or {}
                has_required_args = any(
                    isinstance(arg_def.type, GraphQLNonNull)
                    and getattr(arg_def, "default_value", Undefined) is Undefined
                    for arg_def in field_args.values()
                )
                if has_required_args:
                    continue

                field_named_type = gs.get_named_type(field_def.type)
                if gs.is_leaf_type(field_named_type):
                    selections.append(field_name)
                elif not is_cycle:
                    nested_selection = _build_selection_set_skip_required_args(
                        field_def.type,
                        max_depth=max_depth,
                        depth=depth + 1,
                        _seen_types=seen_with_current,
                    )
                    if nested_selection:
                        selections.append(f"{field_name} {nested_selection}")

        if not selections:
            return "{ __typename }"
        return f"{{ {', '.join(selections)} }}"

    gs._build_selection_set = _build_selection_set_skip_required_args
_patch_graphql_mcp()
# --- End patch ---

from urllib.parse import parse_qs
from graphql_mcp.server import GraphQLMCP
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

_MCP_MAX_TOOL_NAME_LENGTH = 64

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

class _LazyMCPApp:
    """ASGI wrapper that lazily initializes the GraphQLMCP server on the first request.

    The schema is fetched using the bearer token from the first incoming request,
    so no static token is needed at container startup.
    """

    def __init__(self):
        self._app = None
        self._lock = asyncio.Lock()
        self._shutdown_trigger = None
        self._lifespan_done = None
        self._lifespan_task = None

    def _extract_token(self, scope) -> str:
        """Extract bearer token from Authorization header or query string."""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                auth = value.decode()
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
                return auth.strip()
        qs = parse_qs(scope.get("query_string", b"").decode())
        token = qs.get("authorization", [""])[0] if "authorization" in qs else ""
        if token.lower().startswith("bearer "):
            return token[7:].strip()
        return token.strip()

    async def _init_app(self, scope):
        """Fetch schema using the request token and start the inner app's lifespan."""
        # Extract token here (in the event loop) before handing off to an executor
        # thread, so there is no shared-state access across thread boundaries.
        token = self._extract_token(scope)

        # Run the blocking schema-fetch + tool-registration in a thread so the
        # event loop stays responsive while waiting for the remote introspection.
        loop = asyncio.get_running_loop()
        mcp = await loop.run_in_executor(
            None,
            lambda: GraphQLMCP.from_remote_url(
                url=os.environ["API_MCP_BASE_URL"],
                headers={"Authorization": f"Bearer {token}"} if token else {},
                forward_bearer_token=True,
                name=os.environ["API_MCP_SERVER_NAME"],
            ),
        )
        # Drop tools whose names exceed 64 characters (MCP protocol limit)
        for tool in await mcp.list_tools():
            if len(tool.name) > _MCP_MAX_TOOL_NAME_LENGTH:
                mcp.local_provider.remove_tool(tool.name)
        app = mcp.http_app(middleware=[Middleware(AuthFromQueryParam)])

        # Drive the inner app's lifespan as a background task so its session
        # manager is up before we forward any requests to it.
        startup_complete = asyncio.Event()
        lifespan_receive_queue: asyncio.Queue = asyncio.Queue()
        await lifespan_receive_queue.put({"type": "lifespan.startup"})
        lifespan_done = asyncio.Event()

        async def lifespan_receive():
            return await lifespan_receive_queue.get()

        async def lifespan_send(message):
            if message["type"] == "lifespan.startup.complete":
                startup_complete.set()
            elif message["type"] == "lifespan.shutdown.complete":
                lifespan_done.set()

        async def run_lifespan():
            try:
                await app({"type": "lifespan", "asgi": {"version": "3.0"}}, lifespan_receive, lifespan_send)
            finally:
                lifespan_done.set()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await startup_complete.wait()
        except Exception:
            lifespan_task.cancel()
            try:
                await lifespan_task
            except asyncio.CancelledError:
                pass
            raise

        self._lifespan_task = lifespan_task
        self._shutdown_trigger = lifespan_receive_queue
        self._lifespan_done = lifespan_done
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Acknowledge outer lifespan startup immediately — the real MCP
            # server will be started lazily on the first HTTP request.
            msg = await receive()
            if msg["type"] != "lifespan.startup":
                raise ValueError(f"Expected lifespan.startup, got {msg['type']}")
            await send({"type": "lifespan.startup.complete"})

            # Wait for the outer shutdown signal.
            msg = await receive()
            if msg["type"] != "lifespan.shutdown":
                raise ValueError(f"Expected lifespan.shutdown, got {msg['type']}")

            # Propagate shutdown to the inner app if it was ever initialised.
            if self._shutdown_trigger is not None:
                await self._shutdown_trigger.put({"type": "lifespan.shutdown"})
                await self._lifespan_done.wait()

            await send({"type": "lifespan.shutdown.complete"})
            return

        if self._app is None:
            async with self._lock:
                if self._app is None:
                    await self._init_app(scope)

        await self._app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(_LazyMCPApp(), host="0.0.0.0", port=8080)
