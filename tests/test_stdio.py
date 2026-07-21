import subprocess
import sys


def test_stdio_mode_stays_alive():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import asyncio
from fastmcp import FastMCP

mcp = FastMCP("test")

async def _run_stdio():
    await mcp.run_async(transport="stdio")
    await asyncio.Event().wait()

asyncio.run(_run_stdio())
""",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        returncode = proc.wait(timeout=60)
        assert False, f"stdio mode process must not terminate, but exited with code {returncode}"
    except subprocess.TimeoutExpired:
        pass  # process is still alive as expected
    finally:
        proc.terminate()
        proc.wait(timeout=5)
