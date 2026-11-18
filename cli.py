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
        status = await client.call_tool("boss_pipeline_status", {})
        if status.is_error or status.data.get("entrypoint") != "mcp" or not status.data.get("call_id"):
            raise RuntimeError("本地 MCP 自检未能验证工具调用")
        database = await client.call_tool("boss_query_db", {})
        if database.is_error:
            raise RuntimeError("本地 MCP 数据库自检失败")
        print(json.dumps({"status": "ok", "probe": "local_stdio", "call_id": status.data["call_id"],
                          "tool_count": len(tools), "tools": sorted(tool.name for tool in tools)}, ensure_ascii=False, indent=2))


def doctor():
    from browser_install import CHROME_VERSION, configured_executable, executable_version, require_version
    from qr import HAS_QR_DECODER
    from ocr import available_engine
    from config import CHROME_PROFILE_DIR, DATA_DIR
    executable = configured_executable()
    version, error = None, None
    try:
        version = executable_version(executable)
        require_version(version)
    except Exception as failure:
        error = str(failure)
    print(json.dumps({
        "python": sys.version.split()[0], "qr_decoder": HAS_QR_DECODER,
        "chrome": version == CHROME_VERSION,
        "chrome_path": str(executable), "chrome_version": version,
        "required_chrome_version": CHROME_VERSION, "chrome_error": error,
        "local_ocr": available_engine(),
        "data_dir": str(DATA_DIR), "chrome_profile": CHROME_PROFILE_DIR,
    }, ensure_ascii=False, indent=2))
    return 0 if version == CHROME_VERSION and HAS_QR_DECODER else 1


def main():
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="BOSS recruiter local MCP server")
    parser.add_argument("command", nargs="?", choices=("serve", "browser", "check", "doctor", "install-browser"), default="serve")
    arguments = parser.parse_args()
    if arguments.command == "serve":
        from server import mcp
        mcp.run()
    elif arguments.command == "install-browser":
        from browser_install import install_browser
        print(json.dumps(install_browser(), ensure_ascii=False, indent=2))
    elif arguments.command == "doctor":
        raise SystemExit(doctor())
    else:
        asyncio.run(open_browser() if arguments.command == "browser" else check())


if __name__ == "__main__":
    main()
