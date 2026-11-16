import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stdio_can_start_from_another_working_directory(tmp_path):
    transport = StdioTransport(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(tmp_path), env={**os.environ, "BOSS_DATA_DIR": str(tmp_path / "stdio-data")})
    async with Client(transport, timeout=30) as client:
        assert len(await client.list_tools()) == 17
        result = await client.call_tool("boss_pipeline_status", {})
        assert result.data["total"] == 0


@pytest.mark.asyncio
async def test_loopback_http_transport(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log = (tmp_path / "http.log").open("w")
    process = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--transport", "http", "--port", str(port)], env={**os.environ, "BOSS_DATA_DIR": str(tmp_path / "http-data")}, stdout=log, stderr=log)
    try:
        for _ in range(100):
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if process.poll() is not None:
                pytest.fail("HTTP MCP exited during startup")
            await asyncio.sleep(0.1)
        async with Client(f"http://127.0.0.1:{port}/mcp", timeout=30) as client:
            assert len(await client.list_tools()) == 17
            assert (await client.call_tool("boss_query_db", {})).data == []
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()
