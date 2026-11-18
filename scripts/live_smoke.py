"""Read-only website acceptance checks; communication is tested by local fixtures."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[1]


async def run(arguments):
    directory = ROOT / "data" / "live-acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    profile = directory / "profile.yaml"
    profile.write_text("job:\n  title: 功能验收\nscoring:\n  tech_keywords: [Python, SQL]\n", encoding="utf-8")
    transport = StdioTransport(command=sys.executable, args=[str(ROOT / "server.py")],
        env={**os.environ, "BOSS_DATA_DIR": str(directory), "BOSS_PROFILE_FILE": str(profile), "NO_PROXY": "localhost,127.0.0.1,::1"})
    report = {"checked_at": datetime.now().astimezone().isoformat(), "checks": [], "communication": "local-fixture-only"}

    async with Client(transport, timeout=180) as client:
        async def call(name, **parameters):
            result = await client.call_tool(name, parameters)
            data = result.data
            if result.is_error or isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"{name} failed")
            report["checks"].append({"tool": name, "status": "passed"})
            print(name + ": passed", flush=True)
            return data

        assert "boss_export_filter_log" in {tool.name for tool in await client.list_tools()}
        login = await call("boss_login")
        if login.get("status") != "success":
            raise RuntimeError("请先手动登录招聘者账号")
        found = await call("boss_search_candidates", keyword=arguments.keyword, count=arguments.count)
        if found[0].get("error") or not found[0].get("new"):
            raise RuntimeError("验收关键词没有返回新候选人，请换一个关键词或使用新的验收数据目录")
        await call("boss_multi_search", keywords=[arguments.keyword, arguments.secondary], count_per_keyword=1, auto_view=True)
        viewed = await call("boss_view_by_index", index=0, include_ocr=True)
        if not viewed.get("screenshots") or not viewed.get("share_url") or not viewed.get("ocr_text"):
            raise RuntimeError("简历截图、分享链接或本地 OCR 尚未全部通过")
        identifier = viewed["ids"]["expectId"]
        await call("boss_view_by_expect_id", expect_id=identifier)
        linked = await call("boss_query_db", has_share_url=True)
        if not linked:
            raise RuntimeError("分享链接未写入数据库")
        await call("boss_update_candidate", expect_id=identifier, notes="功能验收，仅本地测试数据库", score=60)
        await call("boss_filter_and_score", top_n=2)
        exported = await call("boss_export_report", top_n=2)
        if "候选人报告" not in exported:
            raise RuntimeError("报告导出未通过")
        await call("boss_pipeline_status")
        await call("boss_evaluate_candidate", resume={"skills": ["Python", "SQL"]}, job_requirements="Python SQL")
        await call("boss_debug_page")
        await call("boss_view_candidate", profile_url=viewed["share_url"])
        await call("boss_clear_dedup", expect_ids=[identifier])
        await call("boss_clear_dedup", before_date="2099-01-01")
        await call("boss_reload")
    report["result"] = "passed"
    report["native_ocr_characters"] = len(viewed["ocr_text"])
    report["real_messages_sent"] = 0
    target = directory / "summary.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    target.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="Python")
    parser.add_argument("--secondary", default="SQL")
    parser.add_argument("--count", type=int, default=2)
    asyncio.run(run(parser.parse_args()))
