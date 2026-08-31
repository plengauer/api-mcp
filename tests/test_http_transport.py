import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize("server", ["api-rest-mcp.py", "api-graphql-mcp.py"])
def test_http_transport_is_stateless(server):
    source = Path(__file__).resolve().parent.parent.joinpath(server).read_text()
    tree = ast.parse(source)

    http_app_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "http_app"
    ]

    assert http_app_calls
    assert all(
        any(
            keyword.arg == "stateless_http"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
        for call in http_app_calls
    )
