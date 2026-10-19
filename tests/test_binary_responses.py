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

def _client(handler):
    return httpx2.AsyncClient(
        base_url="http://api.test",
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        event_hooks={"response": [rest._normalize_response]},
    )

def _handler(request):
    if request.url.path == "/redirect":
        return httpx2.Response(302, headers={"location": "https://storage.test/logs.zip"})
    return httpx2.Response(200, content=PAYLOAD, headers={"content-type": "application/zip"})

def test_raw_client_does_not_follow_redirects():
    assert rest.raw_client.follow_redirects is False

@pytest.mark.asyncio
async def test_redirect_surfaces_location():
    response = await _client(_handler).get("/redirect")
    assert response.status_code == 302
    assert "https://storage.test/logs.zip" in response.text

@pytest.mark.asyncio
async def test_binary_body_round_trips():
    response = await _client(_handler).get("/logs")
    assert base64.b64decode(response.text) == PAYLOAD
    assert response.headers["x-original-content-type"] == "application/zip"

@pytest.mark.asyncio
async def test_oversized_binary_rejected(monkeypatch):
    monkeypatch.setattr(rest, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ValueError):
        await _client(_handler).get("/logs")
