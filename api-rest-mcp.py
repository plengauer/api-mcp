import os
import httpx
import uvicorn
from fastmcp import FastMCP


def fix_spec(obj):
    """Remove empty enum arrays from an OpenAPI spec object.

    Some OpenAPI specs contain ``"enum": []`` entries that cause validation
    errors in FastMCP. This function recursively strips those entries so the
    spec can be loaded cleanly.
    """
    if isinstance(obj, dict):
        return {
            k: fix_spec(v)
            for k, v in obj.items()
            if not (k == "enum" and v == [])
        }
    elif isinstance(obj, list):
        return [fix_spec(item) for item in obj]
    return obj


for _var in ["API_MCP_OPENAPI_SPEC_URL", "API_MCP_BASE_URL", "API_MCP_AUTHORIZATION", "API_MCP_SERVER_NAME"]:
    if _var not in os.environ:
        raise EnvironmentError(f"Required environment variable {_var!r} is not set")

try:
    _spec = fix_spec(httpx.get(os.environ["API_MCP_OPENAPI_SPEC_URL"], follow_redirects=True).raise_for_status().json())
except httpx.HTTPError as exc:
    raise RuntimeError(f"Failed to fetch OpenAPI spec from {os.environ['API_MCP_OPENAPI_SPEC_URL']!r}: {exc}") from exc

mcp = FastMCP.from_openapi(
    openapi_spec=_spec,
    client=httpx.AsyncClient(
        base_url=os.environ["API_MCP_BASE_URL"],
        headers={
            "Authorization": os.environ["API_MCP_AUTHORIZATION"]
        }
    ),
    name=os.environ["API_MCP_SERVER_NAME"]
)

if __name__ == "__main__":
    uvicorn.run(mcp.http_app(), host="0.0.0.0", port=8000)
