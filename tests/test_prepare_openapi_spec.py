import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prepare-openapi-spec.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("prepare_openapi_spec", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/a": {
            "get": {
                "operationId": "repos/list-for-user",
                "parameters": [
                    {"name": "x", "in": "query", "schema": {"type": "string", "enum": []}},
                    {"name": "y", "in": "query", "schema": {"type": "string", "enum": ["a"]}},
                ],
            }
        }
    },
}


def test_fix_spec_replaces_slashes_and_drops_empty_enums():
    fixed = _load_script().fix_spec(SPEC)
    operation = fixed["paths"]["/a"]["get"]
    assert operation["operationId"] == "repos_list-for-user"
    assert "enum" not in operation["parameters"][0]["schema"]
    assert operation["parameters"][1]["schema"]["enum"] == ["a"]


def test_main_writes_fixed_spec(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(SPEC))
    output = tmp_path / "openapi.json"
    assert _load_script().main(["prepare", source.as_uri(), str(output)]) == 0
    assert json.loads(output.read_text()) == _load_script().fix_spec(SPEC)


def test_main_rejects_non_openapi_documents(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"hello": "world"}))
    output = tmp_path / "openapi.json"
    assert _load_script().main(["prepare", source.as_uri(), str(output)]) == 1
    assert not output.exists()


def test_download_sends_explicit_user_agent_and_retries(monkeypatch):
    import io
    import urllib.error

    module = _load_script()
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        if len(seen) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        return io.BytesIO(json.dumps(SPEC).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    assert module.download_spec("https://example.invalid/spec.json", sleep=lambda _: None) == SPEC
    assert len(seen) == 3
    assert seen[0].get_header("User-agent", "").startswith("api-mcp-ci")
    assert seen[0].get_header("Accept") == "application/json"


def test_download_does_not_retry_client_errors(monkeypatch):
    import urllib.error

    import pytest

    module = _load_script()
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        module.download_spec("https://example.invalid/spec.json", sleep=lambda _: None)
    assert len(calls) == 1
