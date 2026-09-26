import asyncio
import os
import inspect
import json

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

    # graphql_mcp only forwards the caller's bearer token when FastMCP hands the
    # tool a Context, but the generated tools' signatures never declare one, so
    # ctx is always None and nothing is forwarded. Read the current HTTP request
    # directly instead (outside a request, e.g. stdio, this yields None and the
    # client's base headers apply).
    def _bearer_token_from_current_request(ctx=None):
        try:
            request = gs._get_http_request() if gs._get_http_request else None
        except Exception:
            return None
        if request is None or not hasattr(request, "headers"):
            return None
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        return None
    gs._extract_bearer_token_from_context = _bearer_token_from_current_request

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
import graphql_mcp.server as graphql_mcp_server
from graphql_mcp.server import GraphQLMCP
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

_MCP_MAX_TOOL_NAME_LENGTH = 64


def _mutation_tool_names(mcp):
    """Tool names generated from the schema's Mutation root."""
    schema = getattr(mcp, "schema", None)
    mutation_type = getattr(schema, "mutation_type", None) if schema else None
    if mutation_type is None:
        return set()
    return {
        graphql_mcp_server._to_snake_case(field_name)
        for field_name in mutation_type.fields
    }


def _classify_read_or_write(mcp, tool, mutation_tool_names):
    """Publish a read/write classification for one generated tool.

    graphql_mcp infers readOnlyHint=True for query-backed tools but leaves
    mutation-backed ones with every hint unset, and it never assigns tags. So
    on the wire a mutation is indistinguishable from a tool whose author simply
    said nothing - there is no explicit write classification for a client to
    act on. Mirror what the REST server does: state readOnlyHint outright on
    every tool and tag it "read" or "write".

    A tool counts as read-only only if it is positively known to be one, so
    anything unrecognised is classified as a write rather than silently
    presented as safe.
    """
    annotations = tool.annotations
    read_only = (
        annotations is not None
        and annotations.readOnlyHint is True
        and tool.name not in mutation_tool_names
    )

    def keep(current, fallback):
        return current if current is not None else fallback

    tool.annotations = ToolAnnotations(
        title = annotations.title if annotations is not None else None,
        readOnlyHint = read_only,
        # Hosts treat readOnlyHint and destructiveHint as mutually exclusive
        # flags and leave a tool that asserts neither unclassified, so every
        # mutation states destructiveHint outright. GraphQL draws no
        # create/replace/delete distinction to make it any finer than that.
        destructiveHint = not read_only,
        idempotentHint = keep(
            annotations.idempotentHint if annotations is not None else None,
            True if read_only else False,
        ),
        openWorldHint = keep(
            annotations.openWorldHint if annotations is not None else None,
            True,
        ),
    )
    tool.tags = set(tool.tags or ()) | {"read" if read_only else "write"}


def _finalize_tools(mcp, tools):
    """Drop over-long tool names and classify what remains as read or write."""
    mutation_tool_names = _mutation_tool_names(mcp)
    for tool in tools:
        if len(tool.name) > _MCP_MAX_TOOL_NAME_LENGTH:
            mcp.local_provider.remove_tool(tool.name)
        else:
            _classify_read_or_write(mcp, tool, mutation_tool_names)

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

def _http_app(mcp):
    return mcp.http_app(
        middleware=[Middleware(AuthFromQueryParam)],
        stateless_http=True,
    )


async def _fetch_introspection(headers):
    """Run the standard introspection query live against API_MCP_BASE_URL.

    Same query and checks as graphql_mcp.remote.fetch_remote_schema, but it
    returns the raw result so the MCP is built by the same code as for a baked
    schema, and the token is used for this one request only instead of being
    kept as a default header for every later tool call.
    """
    import aiohttp
    from graphql import get_introspection_query

    url = os.environ["API_MCP_BASE_URL"]
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json={"query": get_introspection_query()},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                raise Exception(
                    f"Failed to fetch schema from {url}: {response.status} - {await response.text()}")
            result = await response.json()
    if "errors" in result:
        raise Exception(f"GraphQL errors during introspection: {result['errors']}")
    if "data" not in result:
        raise Exception(f"No data in introspection response from {url}")
    return result["data"]


def _load_baked_introspection():
    """Return the introspection result baked into the image, or None.

    The path comes from API_MCP_GRAPHQL_SCHEMA_PATH. CI only produces the file
    for APIs whose introspection it can run at build time (GitHub, with the
    Actions token), so a missing file is expected and means "introspect live".
    A file that exists but is unreadable is a broken build and fails loudly.

    Accepts both the raw HTTP response shape ``{"data": {"__schema": ...}}``
    and the bare ``{"__schema": ...}`` shape.
    """
    path = os.environ.get("API_MCP_GRAPHQL_SCHEMA_PATH", "")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        introspection = json.load(f)
    if isinstance(introspection, dict) and isinstance(introspection.get("data"), dict):
        introspection = introspection["data"]
    if not isinstance(introspection, dict) or "__schema" not in introspection:
        raise ValueError(f"{path} does not contain a GraphQL introspection result")
    return introspection


def _prime_remote_client(client, introspection):
    """Feed the introspection result to the client's result-shaping cache.

    RemoteGraphQLClient introspects the endpoint again on the first tool call
    (with its base headers only) to learn which fields are lists, so it can
    turn null lists into []. Without a base token GitHub rejects that request
    and the client silently falls back to guessing. Answer it from the result
    we already have instead: same parsing code, no network, no token.
    """
    async def _baked(query, variables=None):
        return introspection

    client._raw_execute_request = _baked
    try:
        # Nothing in here awaits real I/O, so drive the coroutine to completion
        # directly; this works with or without a running event loop.
        coro = client._introspect_schema()
        try:
            coro.send(None)
        except StopIteration:
            pass
        else:
            coro.close()
            raise RuntimeError("priming the GraphQL client unexpectedly suspended")
    finally:
        del client._raw_execute_request


def _build_mcp_from_introspection(introspection, headers):
    """Build the MCP from an introspection result (baked or fetched live).

    Mirrors graphql_mcp.server.build_remote_mcp (what from_remote_url calls),
    minus the introspection request: tools execute remotely against
    API_MCP_BASE_URL and forward the caller's bearer token. ``headers`` become
    the client's default headers (only stdio sets any: HTTP_AUTHORIZATION).
    """
    from graphql import build_client_schema
    from graphql_mcp.remote import RemoteGraphQLClient

    url = os.environ["API_MCP_BASE_URL"]
    schema = build_client_schema(introspection)
    mcp = GraphQLMCP(
        schema=schema,
        register_tools=False,
        name=os.environ["API_MCP_SERVER_NAME"],
    )
    client = RemoteGraphQLClient(url, dict(headers))
    mcp.remote_client = client
    _prime_remote_client(client, introspection)
    graphql_mcp_server.add_tools_from_schema_with_remote(
        schema, mcp, client,
        forward_bearer_token=True,
    )
    return mcp


def _log(message):
    print(json.dumps({"severity": "INFO", "message": message}), flush=True)


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

        # Fetch asynchronously, then run the CPU-heavy tool registration in a
        # thread so the event loop stays responsive.
        introspection = await _fetch_introspection(
            {"Authorization": f"Bearer {token}"} if token else {}
        )
        loop = asyncio.get_running_loop()
        mcp = await loop.run_in_executor(
            None, lambda: _build_mcp_from_introspection(introspection, {})
        )
        # Drop tools whose names exceed 64 characters (MCP protocol limit)
        # and tag/annotate the rest as read or write.
        _finalize_tools(mcp, await mcp.list_tools())
        app = _http_app(mcp)

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

class _LogErrorBodies:
    """Minimal ASGI wrapper: on status >= 400 (or no response) print request/response bodies as one JSON line."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        req, res, status = bytearray(), bytearray(), None

        async def rcv():
            m = await receive()
            if m["type"] == "http.request":
                req.extend(m.get("body", b"")); del req[8192:]
            return m

        async def snd(m):
            nonlocal status
            if m["type"] == "http.response.start":
                status = m["status"]
            elif m["type"] == "http.response.body":
                res.extend(m.get("body", b"")); del res[8192:]
            await send(m)

        try:
            await self.app(scope, rcv, snd)
        finally:
            if status is None or status == 400:
                print(json.dumps({
                    "severity": "ERROR",
                    "message": f"{scope['method']} {scope['path']} -> {status}",
                    "protocol_version": dict(scope["headers"]).get(b"mcp-protocol-version", b"").decode(),
                    "request_body": req.decode("utf-8", "replace"),
                    "response_body": res.decode("utf-8", "replace"),
                }), flush=True)

if __name__ == "__main__":
    mode = os.environ.get("API_MCP_MODE", "http")
    introspection = _load_baked_introspection()
    if mode == "stdio":
        http_auth = os.environ.get("HTTP_AUTHORIZATION", "")
        headers = {"Authorization": http_auth} if http_auth else {}
        if introspection is not None:
            mcp = _build_mcp_from_introspection(introspection, headers)
        else:
            mcp = _build_mcp_from_introspection(
                asyncio.run(_fetch_introspection(headers)), headers
            )
        _finalize_tools(mcp, asyncio.run(mcp.list_tools()))
        mcp.run()
    else:
        import uvicorn
        if introspection is not None:
            # No token needed to build from the baked schema, so do it now:
            # the first request then only pays for the request itself.
            mcp = _build_mcp_from_introspection(introspection, {})
            _finalize_tools(mcp, asyncio.run(mcp.list_tools()))
            _log(f"MCP built eagerly from baked schema {os.environ['API_MCP_GRAPHQL_SCHEMA_PATH']}")
            app = _http_app(mcp)
        else:
            _log("No baked schema, MCP will be built from live introspection on the first request")
            app = _LazyMCPApp()
        uvicorn.run(_LogErrorBodies(app), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
