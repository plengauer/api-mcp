import os
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
