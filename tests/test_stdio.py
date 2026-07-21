import subprocess
import sys

import pytest


STDIO_BLOCKING_TIMEOUT_SECONDS = 180


def test_stdio_mode_stays_alive_while_stdin_open():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
from fastmcp import FastMCP

FastMCP("test").run(transport="stdio")
""",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=STDIO_BLOCKING_TIMEOUT_SECONDS)

        assert proc.stdin is not None
        proc.stdin.close()
        returncode = proc.wait(timeout=30)
        assert returncode == 0, f"stdio mode process exited with {returncode} after stdin closed"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
