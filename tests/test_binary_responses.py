import base64
import importlib.util
import os
import pathlib

import httpx2
import pytest

os.environ.setdefault("API_MCP_BASE_URL", "http://api.test")
os.environ.setdefault("API_MCP_SERVER_NAME", "test")
_spec = importlib.util.spec_from_file_location("api_rest_mcp", pathlib.Path(__file__).parent.parent / "api-rest-mcp.py")
rest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rest)

PAYLOAD = bytes(range(256)) * 50


def _response(status, **kwargs):
    return httpx2.Response(status, request=httpx2.Request("GET", "http://api.test/x"), **kwargs)


def test_raw_client_does_not_follow_redirects():
    assert rest.raw_client.follow_redirects is False


def test_redirect_is_surfaced_untouched():
    result = rest._response_result(_response(302, headers={"location": "https://storage.test/logs.zip"}))
    assert result["status_code"] == 302
    assert result["headers"]["location"] == "https://storage.test/logs.zip"


def test_binary_body_round_trips():
    result = rest._response_result(_response(200, content=PAYLOAD, headers={"content-type": "application/zip"}))
    assert result["body_encoding"] == "base64"
    assert base64.b64decode(result["body"]) == PAYLOAD
    assert result["headers"]["content-type"] == "application/zip"


def test_text_body_unchanged():
    result = rest._response_result(_response(200, json={"a": 1}))
    assert "body_encoding" not in result
    assert result["body"] == '{"a":1}' or result["body"] == '{"a": 1}'


def test_oversized_response_rejected(monkeypatch):
    monkeypatch.setattr(rest, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ValueError):
        rest._response_result(_response(200, content=PAYLOAD, headers={"content-type": "application/zip"}))
