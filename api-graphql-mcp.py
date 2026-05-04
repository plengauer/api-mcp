import os
from graphql_mcp.server import GraphQLMCP


mcp = GraphQLMCP.from_remote_url(
    url=os.environ["API_MCP_BASE_URL"],
    headers={
        "Authorization": os.environ["API_MCP_AUTHORIZATION"]
    },
    name=os.environ["API_MCP_SERVER_NAME"]
)

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
