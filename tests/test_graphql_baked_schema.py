"""In-process tests for building the GraphQL MCP from a baked introspection result.

These need the server's own dependencies (requirements.graphql.txt) and are
skipped where only tests/requirements.txt is installed.
"""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("graphql_mcp")

from graphql import build_schema, introspection_from_schema  # noqa: E402

SDL = """
type Query {
  viewer: User!
  repository(owner: String!, name: String!): Repository
}
type Mutation {
  addStar(starrableId: ID!): Repository
}
type User {
  login: String!
  repositories: [Repository!]!
}
type Repository {
  name: String!
  topics: [String]
}
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import os
    os.environ.setdefault("API_MCP_BASE_URL", "https://example.invalid/graphql")
    os.environ.setdefault("API_MCP_SERVER_NAME", "Test GraphQL API")
    path = Path(__file__).resolve().parent.parent / "api-graphql-mcp.py"
    spec = importlib.util.spec_from_file_location("api_graphql_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def introspection():
    return introspection_from_schema(build_schema(SDL))


@pytest.mark.parametrize("wrap", [False, True], ids=["bare", "data-envelope"])
def test_load_baked_introspection_accepts_both_shapes(server, introspection, tmp_path, monkeypatch, wrap):
    path = tmp_path / "introspection.json"
    path.write_text(json.dumps({"data": introspection} if wrap else introspection))
    monkeypatch.setenv("API_MCP_GRAPHQL_SCHEMA_PATH", str(path))
    assert server._load_baked_introspection() == introspection


def test_load_baked_introspection_missing_file_means_live(server, tmp_path, monkeypatch):
    monkeypatch.setenv("API_MCP_GRAPHQL_SCHEMA_PATH", str(tmp_path / "absent.json"))
    assert server._load_baked_introspection() is None
    monkeypatch.delenv("API_MCP_GRAPHQL_SCHEMA_PATH")
    assert server._load_baked_introspection() is None


def test_load_baked_introspection_rejects_garbage(server, tmp_path, monkeypatch):
    path = tmp_path / "introspection.json"
    path.write_text(json.dumps({"data": {"nope": 1}}))
    monkeypatch.setenv("API_MCP_GRAPHQL_SCHEMA_PATH", str(path))
    with pytest.raises(ValueError):
        server._load_baked_introspection()


def test_build_from_introspection(server, introspection):
    mcp = server._build_mcp_from_introspection(introspection, {})
    tools = asyncio.run(mcp.list_tools())
    server._finalize_tools(mcp, tools)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert {"viewer", "repository", "add_star"} <= tools.keys()
    for name in ("viewer", "repository"):
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.destructive_hint is False
        assert "read" in tools[name].tags
    assert tools["add_star"].annotations.read_only_hint is False
    assert tools["add_star"].annotations.destructive_hint is True
    assert "write" in tools["add_star"].tags

    # Tools execute remotely without a baked-in token; the caller's is forwarded.
    client = mcp.remote_client
    assert client.url == "https://example.invalid/graphql"
    assert "Authorization" not in client.headers
    # The client's list-field cache comes from the baked schema, so the first
    # tool call does not introspect the endpoint again.
    assert client._introspected is True
    assert client._array_fields_cache["User"]["repositories"] is True
    assert client._array_fields_cache["Repository"]["name"] is False


def test_bearer_token_extraction_outside_request_is_none(server):
    import graphql_mcp.server as gs
    assert gs._extract_bearer_token_from_context(None) is None
