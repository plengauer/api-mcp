import os
import uvicorn
from graphql_mcp.server import GraphQLMCP


for _var in ["API_MCP_BASE_URL", "API_MCP_AUTHORIZATION", "API_MCP_SERVER_NAME"]:
    if _var not in os.environ:
        raise EnvironmentError(f"Required environment variable {_var!r} is not set")

mcp = GraphQLMCP.from_remote_url(
    url=os.environ["API_MCP_BASE_URL"],
    headers={
        "Authorization": os.environ["API_MCP_AUTHORIZATION"]
    },
    name=os.environ["API_MCP_SERVER_NAME"]
)

if __name__ == "__main__":
    uvicorn.run(mcp.http_app(), host="0.0.0.0", port=8000)
