import importlib.util
import os
import pathlib

os.environ.setdefault("API_MCP_BASE_URL", "http://api.test")
os.environ.setdefault("API_MCP_SERVER_NAME", "test")
_spec = importlib.util.spec_from_file_location("api_rest_mcp", pathlib.Path(__file__).parent.parent / "api-rest-mcp.py")
rest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rest)


def test_raw_client_does_not_follow_redirects():
    assert rest.raw_client.follow_redirects is False
