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
