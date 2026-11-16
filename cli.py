"""Command-line entry points for the local MCP project."""

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def open_browser():
    from browser import BossBrowser
    browser = BossBrowser()
    try:
        await browser.launch()
        if await browser.is_logged_in():
            await browser.page.bring_to_front()
            result = {"status": "success", "message": "招聘者账号已登录，已打开当前窗口。"}
        else:
            result = await browser.login()
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await browser.close()


async def check():
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    transport = StdioTransport(command=sys.executable, args=[str(Path(__file__).with_name("server.py"))])
    async with Client(transport, timeout=45) as client:
        tools = await client.list_tools()
        for name in ("boss_pipeline_status", "boss_query_db"):
            await client.call_tool(name, {})
        print(json.dumps({"status": "ok", "tool_count": len(tools), "tools": sorted(tool.name for tool in tools)}, ensure_ascii=False, indent=2))


def doctor():
    import shutil
    from qr import HAS_QR_DECODER
    from ocr import available_engine
    from config import CHROME_PROFILE_DIR, DATA_DIR
    print(json.dumps({
        "python": sys.version.split()[0], "qr_decoder": HAS_QR_DECODER,
        "chrome": Path("/Applications/Google Chrome.app").exists() if sys.platform == "darwin" else bool(shutil.which("google-chrome") or shutil.which("chromium")),
        "local_ocr": available_engine(),
        "data_dir": str(DATA_DIR), "chrome_profile": CHROME_PROFILE_DIR,
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="BOSS recruiter local MCP server")
    parser.add_argument("command", nargs="?", choices=("serve", "browser", "check", "doctor"), default="serve")
    arguments = parser.parse_args()
    if arguments.command == "serve":
        from server import mcp
        mcp.run()
    elif arguments.command == "doctor":
        doctor()
    else:
        asyncio.run(open_browser() if arguments.command == "browser" else check())


if __name__ == "__main__":
    main()
