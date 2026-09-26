"""Run GraphQL introspection at build time and write the result for the image.

Usage: fetch-graphql-schema.py OUTPUT_PATH
Env:   GRAPHQL_URL    endpoint to introspect
       GRAPHQL_TOKEN  optional bearer token (GitHub requires one)

Writes the introspection result as {"__schema": ...}. The container builds its
MCP from this file at startup instead of introspecting on the first request.
Uses graphql-core's standard introspection query (the one graphql_mcp sends at
runtime) and checks that the result builds a schema, so a broken dump fails
the build rather than the container.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from graphql import build_client_schema, get_introspection_query

ATTEMPTS = 3


def fetch(url, token):
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": get_introspection_query()}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "api-mcp-build",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main():
    output = sys.argv[1]
    url = os.environ["GRAPHQL_URL"]
    token = os.environ.get("GRAPHQL_TOKEN", "")
    for attempt in range(1, ATTEMPTS + 1):
        try:
            result = fetch(url, token)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            retryable = not isinstance(e, urllib.error.HTTPError) or e.code >= 500 or e.code == 429
            if attempt == ATTEMPTS or not retryable:
                raise
            print(f"introspection attempt {attempt} failed: {e}; retrying", file=sys.stderr)
            time.sleep(5 * attempt)
    if result.get("errors"):
        sys.exit(f"GraphQL errors during introspection: {result['errors']}")
    introspection = result.get("data")
    if not isinstance(introspection, dict) or "__schema" not in introspection:
        sys.exit("introspection response has no data.__schema")
    schema = build_client_schema(introspection)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(introspection, f, separators=(",", ":"))
    print(f"wrote {output}: {len(schema.type_map)} types, {os.path.getsize(output)} bytes")


if __name__ == "__main__":
    main()
