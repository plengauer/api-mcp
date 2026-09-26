"""Download an OpenAPI spec and apply every fix the REST MCP server needs.

Runs in CI (see .github/workflows/publish.yml) so the fixed spec can be baked
into the REST container at build time instead of being downloaded and fixed
on every container start.

Usage: python3 scripts/prepare-openapi-spec.py <spec-url> <output-path>

Only uses the Python standard library so it runs on a plain CI runner.
"""
import json
import sys
import urllib.request


def fix_spec(obj):
    """Make a spec digestible for FastMCP.from_openapi.

    * operationIds may contain '/', which is not valid in an MCP tool name, so
      it is replaced with '_'.
    * Empty enums (enum: []) cannot be satisfied by any value and break schema
      generation, so they are dropped.
    """
    if isinstance(obj, dict):
        return {
            k: (
                v.replace("/", "_") if k == "operationId" and isinstance(v, str)
                else fix_spec(v)
            )
            for k, v in obj.items()
            if not (k == "enum" and v == [])
        }
    elif isinstance(obj, list):
        return [fix_spec(item) for item in obj]
    return obj


def download_spec(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def main(argv):
    if len(argv) != 3:
        print(f"usage: {argv[0]} <spec-url> <output-path>", file=sys.stderr)
        return 2
    url, output_path = argv[1], argv[2]

    spec = download_spec(url)
    if not isinstance(spec, dict) or not ("openapi" in spec or "swagger" in spec) or not spec.get("paths"):
        print(f"{url} does not look like an OpenAPI document with paths", file=sys.stderr)
        return 1

    spec = fix_spec(spec)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, separators=(",", ":"))

    print(f"wrote fixed OpenAPI spec from {url} to {output_path} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
