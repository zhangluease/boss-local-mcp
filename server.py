"""BOSS 直聘招聘者 MCP Server — 候选人筛选与评估."""

import sys
import os
import importlib
import asyncio
from functools import wraps
from pathlib import Path
from fastmcp import FastMCP
import browser as browser_mod
import scraper as scraper_mod
import evaluator as evaluator_mod
import candidate_db as candidate_db_mod
from browser import BossBrowser
from scraper import BossScraper
from evaluator import CandidateEvaluator
from candidate_db import CandidateDB
from config import DEDUP_FILE, CANDIDATE_DB_FILE, PROFILE

mcp = FastMCP("boss-local-mcp")

# Shared instances
_browser: BossBrowser | None = None
_scraper: BossScraper | None = None
_evaluator = CandidateEvaluator()

# --- Candidate database (replaces bare-ID dedup set) ---

_db = CandidateDB(CANDIDATE_DB_FILE, legacy_dedup_path=DEDUP_FILE)
_browser_lock = asyncio.Lock()


def browser_operation(function):
    @wraps(function)
    async def serialized(*args, **kwargs):
        async with _browser_lock:
            return await function(*args, **kwargs)
    return serialized


async def add_local_ocr(result):
    from ocr import ocr_image
    texts = []
    try:
        for screenshot in result.get("screenshots", []):
            texts.append(await asyncio.to_thread(ocr_image, Path(screenshot).read_bytes()))
        result["ocr_text"] = "\n\n".join(texts)
    except Exception as error:
        result["ocr_error"] = str(error)
    return result


# --- Browser/scraper helpers ---

async def get_browser() -> BossBrowser:
    global _browser, _scraper
    if _browser is None or not _browser.is_alive:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
        _browser = BossBrowser()
        await _browser.launch()
        _scraper = None
    return _browser


async def get_scraper() -> BossScraper:
    global _scraper
    browser = await get_browser()
    if _scraper is None:
        _scraper = BossScraper(browser)
    return _scraper


# --- Tools ---

@mcp.tool()
@browser_operation
async def boss_login() -> dict:
    """登录 BOSS 直聘招聘者账号。

    首次使用返回 waiting_for_login，由用户手动扫码或短信登录。
    等待扫码前断开调试连接并保留 Chrome，不轮询或刷新页面。
    返回 waiting_for_login 后等待用户完成；再次调用会重新连接、检查并保存 Cookie。
    """
    global _browser, _scraper
    browser = await get_browser()
    if await browser.is_logged_in():
        await browser._save_cookies()
        return {"status": "success", "message": "已登录，Cookie 有效"}
    try:
        result = await browser.login()
    finally:
        # Match the working CLI handoff: leave Chrome open without keeping
        # a debugger session attached while the user scans the QR code.
        try:
            await browser.close()
        finally:
            _browser = None
            _scraper = None
    return {
        **result,
        "browser_connection": "disconnected",
        "next_action": "wait_for_user",
        "message": "扫码页面已打开，调试连接已断开，Chrome 窗口会保留。请直接在 Chrome 中手动扫码；完成后再调用 boss_login 检查登录状态，等待期间不要刷新或使用其他浏览器工具接管页面。",
    }


@mcp.tool()
@browser_operation
async def boss_search_candidates(
    keyword: str,
    city: str = "",
    experience: str = "",
    salary: str = "",
    count: int = 30,
) -> list[dict]:
    """在 BOSS 直聘「找人才」页面搜索候选人。

    支持多次搜索自动去重：重复调用会刷新结果，只返回新候选人。
    所有候选人数据自动持久化到本地数据库。

    Args:
        keyword: 搜索关键词，如 "产品经理"、"前端工程师"
        city: 城市，如 "杭州"、"北京"（可选）
        experience: 经验要求，如 "3-5年"（可选）
        salary: 期望薪资范围（可选）
        count: 期望获取数量，默认 30，传入更大值会自动滚动加载（如 100、200）

    Returns:
        候选人列表（已去重），附带 _stats 统计信息
    """
    scraper = await get_scraper()
    browser = await get_browser()

    try:
        all_candidates = await scraper.search_candidates(keyword, city, experience, salary, count)
    except Exception as e:
        # Check if it's a verification/CAPTCHA issue
        verify = await browser.check_and_screenshot_verification()
        if verify:
            return [verify]
        return [{"error": str(e), "message": "搜索失败，请检查浏览器状态"}]

    # Check for error responses from scraper
    if all_candidates and isinstance(all_candidates[0], dict) and all_candidates[0].get("error"):
        verify = await browser.check_and_screenshot_verification()
        if verify:
            return [verify]
        return all_candidates

    new_candidates = []
    for c in all_candidates:
        eid = c.get("expectId", "")
        if not eid or _db.has(eid):
            continue
        _db.add(c, source_keyword=keyword)
        new_candidates.append(c)

    if new_candidates:
        _db._save()

    stats = {
        "_stats": True,
        "total_fetched": len(all_candidates),
        "new": len(new_candidates),
        "duplicates": len(all_candidates) - len(new_candidates),
        "cumulative_seen": _db.stats()["total"],
        "filter_scope": "loaded_candidates" if city or experience or salary else "none",
    }
    return [stats] + new_candidates


@mcp.tool()
@browser_operation
async def boss_multi_search(
    keywords: list[str] = None,
    city: str = "",
    experience: str = "",
    count_per_keyword: int = 50,
    auto_view: bool = True,
) -> list[dict]:
    """多关键词批量搜索候选人，自动去重，自动获取分享链接。

    按顺序执行多个关键词搜索，跨关键词去重，返回所有新候选人的合并结果。
    如果不传 keywords 和 city，自动从 search_profile.yaml 配置文件读取。
    所有候选人数据自动持久化到本地数据库。

    当 auto_view=True 时，每个关键词搜索后会自动对所有新候选人执行 view_by_index
    获取 share_url（zpurl.cn 永久链接），确保切换关键词前链接已保存。

    Args:
        keywords: 搜索关键词列表（可选，默认从配置文件读取）
        city: 城市，所有搜索共用（可选，默认从配置文件读取）
        experience: 经验要求，所有搜索共用（可选）
        count_per_keyword: 每个关键词期望获取数量，默认 50
        auto_view: 搜索时自动获取所有新候选人的分享链接，默认 True

    Returns:
        统一结果列表：[总stats, 分词stats..., 候选人...]
    """
    import logging
    log = logging.getLogger("boss-server")

    # Defaults from profile
    if keywords is None:
        keywords = PROFILE.get("keywords", [])
    if not keywords or not all(isinstance(keyword, str) and keyword.strip() for keyword in keywords):
        raise ValueError("请提供非空 keywords，或先在 search_profile.yaml 中配置关键词")
    if not 1 <= count_per_keyword <= 300:
        raise ValueError("count_per_keyword 必须在 1 到 300 之间")
    if not city:
        city = PROFILE.get("job", {}).get("city", "")

    initial_total = _db.stats()["total"]
    scraper = await get_scraper()
    all_new_candidates = []
    per_keyword_stats = []
    failures = []

    browser = await get_browser()

    for i, kw in enumerate(keywords):
        try:
            raw = await scraper.search_candidates(kw, city, experience, "", count_per_keyword)
        except Exception as e:
            # Check for verification CAPTCHA
            verify = await browser.check_and_screenshot_verification()
            if verify:
                # Return partial results + verification notice
                verify["_partial"] = True
                verify["completed_keywords"] = i
                return [verify] + per_keyword_stats + all_new_candidates
            log.warning(f"search '{kw}' failed: {e}")
            failures.append({"keyword": kw, "message": str(e)})
            continue

        # Check for error responses
        if raw and isinstance(raw[0], dict) and raw[0].get("error"):
            verify = await browser.check_and_screenshot_verification()
            if verify:
                verify["_partial"] = True
                verify["completed_keywords"] = i
                return [verify] + per_keyword_stats + all_new_candidates
            log.warning(f"search '{kw}' returned error: {raw[0].get('error')}")
            failures.append({"keyword": kw, "message": raw[0]["error"]})
            continue

        new_for_kw = []
        for c in raw:
            eid = c.get("expectId", "")
            if not eid or _db.has(eid):
                continue
            _db.add(c, source_keyword=kw)
            c["_source_keyword"] = kw
            new_for_kw.append(c)

        # Auto-view: get share_url for ALL new candidates before switching keyword
        viewed = 0
        view_failed = 0
        if auto_view and new_for_kw:
            for c in new_for_kw:
                idx = c.get("index")
                if idx is None:
                    continue
                try:
                    result = await scraper.view_candidate_by_index(idx)
                    if result.get("error"):
                        view_failed += 1
                        continue
                    share_url = result.get("share_url", "")
                    eid = c.get("expectId", "")
                    if eid and share_url:
                        _db.update(eid, share_url=share_url)
                        c["share_url"] = share_url
                    viewed += 1
                except Exception as e:
                    view_failed += 1
                    log.warning(f"view_by_index({idx}) failed for {c.get('name', '?')}: {e}")
                    # Check for verification on view failure
                    verify = await browser.check_and_screenshot_verification()
                    if verify:
                        verify["_partial"] = True
                        verify["completed_keywords"] = i + 1
                        verify["viewed_before_stop"] = viewed
                        _db._save()
                        return [verify] + per_keyword_stats + all_new_candidates

        kw_stat = {
            "_keyword_stats": True,
            "keyword": kw,
            "order": i + 1,
            "fetched": len(raw),
            "new": len(new_for_kw),
            "duplicates": len(raw) - len(new_for_kw),
        }
        if auto_view:
            kw_stat["viewed"] = viewed
            kw_stat["view_failed"] = view_failed
            kw_stat["share_links"] = sum(bool(candidate.get("share_url")) for candidate in new_for_kw)
        per_keyword_stats.append(kw_stat)
        all_new_candidates.extend(new_for_kw)

        # Save after each keyword to avoid data loss on interruption
        if new_for_kw:
            _db._save()

    current_total = _db.stats()["total"]
    summary = {
        "_stats": True,
        "_multi_search": True,
        "errors": failures,
        "keywords_count": len(keywords),
        "total_new": len(all_new_candidates),
        "total_fetched": sum(s["fetched"] for s in per_keyword_stats),
        "total_duplicates": sum(s["duplicates"] for s in per_keyword_stats),
        "cumulative_seen": current_total,
        "new_this_session": current_total - initial_total,
    }
    if auto_view:
        summary["total_viewed"] = sum(s.get("viewed", 0) for s in per_keyword_stats)
        summary["total_view_failed"] = sum(s.get("view_failed", 0) for s in per_keyword_stats)

    return [summary] + per_keyword_stats + all_new_candidates


@mcp.tool()
async def boss_clear_dedup(
    expect_ids: list[str] = None,
    status: str = "",
    before_date: str = "",
) -> dict:
    """选择性清除去重记录。

    Args:
        expect_ids: 指定要清除的候选人ID列表（可选）
        status: 清除指定状态的所有候选人（可选，如 "legacy"、"new"）
        before_date: 清除此日期之前的记录（可选，格式 YYYY-MM-DD）

    如果所有参数都为空，清除全部记录。

    Returns:
        清除结果
    """
    if expect_ids:
        removed = _db.remove_ids(expect_ids)
    elif status:
        removed = _db.remove_by_status(status)
    elif before_date:
        removed = _db.remove_before_date(before_date)
    else:
        removed = _db.clear_all()

    return {
        "status": "success",
        "cleared": removed,
        "remaining": _db.stats()["total"],
        "message": f"已清除 {removed} 条记录",
    }


@mcp.tool()
@browser_operation
async def boss_view_candidate(profile_url: str) -> dict:
    """查看候选人详细简历。

    Args:
        profile_url: 候选人的 BOSS 直聘个人页面 URL

    Returns:
        结构化简历数据
    """
    scraper = await get_scraper()
    matches = [candidate for candidate in _db.query(has_share_url=True, limit=100000)
               if candidate.get("share_url") == profile_url]
    if matches:
        visible = await scraper.get_visible_expect_ids()
        for candidate in matches:
            if candidate["expectId"] in visible:
                result = await scraper.view_candidate_by_index(visible.index(candidate["expectId"]))
                return {"profile_url": profile_url, **result}
    return await scraper.view_candidate(profile_url)


@mcp.tool()
@browser_operation
async def boss_view_by_index(index: int, include_ocr: bool = False) -> dict:
    """点击搜索结果中第 N 个候选人，查看详细简历并提取分享链接。

    必须先执行 boss_search_candidates 搜索，然后用返回结果中的 index 字段调用此工具。
    获取到的 share_url 会自动存入候选人数据库。

    Args:
        index: 候选人在搜索结果中的索引（0-based，来自搜索结果的 index 字段）

    Returns:
        候选人简历截图路径 + 分享链接
    """
    scraper = await get_scraper()
    result = await scraper.view_candidate_by_index(index)

    # Auto-save share_url to database
    share_url = result.get("share_url", "")
    ids = result.get("ids", {})
    expect_id = ids.get("expectId", "")
    if expect_id and share_url:
        _db.update(expect_id, share_url=share_url, status="viewed")
    elif expect_id:
        _db.update(expect_id, status="viewed")

    return await add_local_ocr(result) if include_ocr else result


@mcp.tool()
@browser_operation
async def boss_view_by_expect_id(expect_id: str, include_ocr: bool = False) -> dict:
    """通过 expectId 在当前搜索页面查找候选人并查看简历。

    自动在当前页面的候选人卡片中查找匹配的 expectId，
    找到后点击查看简历并提取分享链接。

    Args:
        expect_id: 候选人的 expectId

    Returns:
        候选人简历截图路径 + 分享链接，或错误信息
    """
    scraper = await get_scraper()
    visible = await scraper.get_visible_expect_ids()
    if expect_id not in visible:
        return {
            "error": f"候选人 {expect_id} 不在当前搜索页面上（共 {len(visible)} 张卡片）",
            "visible_count": len(visible),
        }
    index = visible.index(expect_id)
    result = await scraper.view_candidate_by_index(index)

    share_url = result.get("share_url", "")
    ids = result.get("ids", {})
    eid = ids.get("expectId", expect_id)
    if eid and share_url:
        _db.update(eid, share_url=share_url, status="viewed")
    elif eid:
        _db.update(eid, status="viewed")

    return await add_local_ocr(result) if include_ocr else result


@mcp.tool()
@browser_operation
async def boss_greet_by_index(index: int, message: str = "") -> dict:
    """在搜索结果中直接对候选人打招呼/发起沟通。

    点击候选人卡片后，自动点击「联系Ta」按钮。
    打招呼后候选人会进入 BOSS 直聘的「沟通」列表，可永久找到。

    Args:
        index: 候选人在搜索结果中的索引（0-based）
        message: 自定义招呼消息（可选）

    Returns:
        发送结果，包含候选人标识符（geekId, expectId）
    """
    scraper = await get_scraper()
    result = await scraper.greet_by_index(index, message)

    # Auto-update status in database
    ids = result.get("ids", {})
    expect_id = ids.get("expectId", "")
    if expect_id and (result.get("status") == "success" or result.get("greeting_sent")):
        _db.update(expect_id, status="greeted")

    return result


@mcp.tool()
async def boss_evaluate_candidate(
    resume: dict,
    job_requirements: str = "",
) -> dict:
    """按岗位关键词对简历进行可解释的相关性匹配，分数供人工复核。

    Args:
        resume: 候选人简历数据
        job_requirements: 岗位要求文本（可选，默认使用配置文件中的 JD）

    Returns:
        评估结果：score (0-100), strengths, weaknesses, recommendation, summary
    """
    return _evaluator.evaluate(resume, job_requirements)


@mcp.tool()
@browser_operation
async def boss_send_greeting(
    profile_url: str,
    message: str = "",
) -> dict:
    """向候选人发送打招呼消息。

    Args:
        profile_url: 候选人的 BOSS 直聘个人页面 URL
        message: 自定义消息内容（可选）

    Returns:
        发送结果
    """
    scraper = await get_scraper()
    return await scraper.send_greeting(profile_url, message)


# --- New tools: Database query & management ---

@mcp.tool()
async def boss_query_db(
    status: str = "",
    has_share_url: bool | None = None,
    keyword: str = "",
    date_from: str = "",
    limit: int = 50,
) -> list[dict]:
    """查询候选人数据库，支持按状态/链接/关键词/日期筛选。

    不需要浏览器连接，直接从本地数据库读取。

    Args:
        status: 按状态筛选（new/viewed/shortlisted/greeted/rejected/legacy）
        has_share_url: True=只返回有链接的, False=只返回没链接的
        keyword: 按搜索来源关键词筛选
        date_from: 只返回此日期之后的候选人（YYYY-MM-DD）
        limit: 最大返回数量，默认 50

    Returns:
        候选人列表
    """
    return _db.query(
        status=status or None,
        has_share_url=has_share_url,
        source_keyword=keyword or None,
        date_from=date_from or None,
        limit=limit,
    )


@mcp.tool()
async def boss_update_candidate(
    expect_id: str,
    status: str = "",
    score: int | None = None,
    notes: str = "",
    share_url: str = "",
) -> dict:
    """更新候选人状态、评分或备注。

    Claude 在对话中评估候选人后，调用此工具将评分结果写回数据库。

    Args:
        expect_id: 候选人 expectId
        status: 新状态（new/viewed/shortlisted/greeted/rejected）
        score: 匹配度评分（0-100）
        notes: 备注信息
        share_url: 分享链接

    Returns:
        更新结果
    """
    fields = {}
    allowed_statuses = {"new", "viewed", "shortlisted", "greeted", "rejected", "legacy"}
    if status and status not in allowed_statuses:
        return {"status": "error", "message": "不支持的候选人状态"}
    if score is not None and not 0 <= score <= 100:
        return {"status": "error", "message": "score 必须在 0 到 100 之间"}
    if status:
        fields["status"] = status
    if score is not None:
        fields["score"] = score
    if notes:
        fields["notes"] = notes
    if share_url:
        fields["share_url"] = share_url

    if not fields:
        return {"status": "error", "message": "没有需要更新的字段"}

    if _db.update(expect_id, **fields):
        return {"status": "success", "expect_id": expect_id, "updated": fields}
    return {"status": "error", "message": f"未找到候选人 {expect_id}"}


@mcp.tool()
async def boss_pipeline_status() -> dict:
    """查看当前招聘流水线进度。

    返回数据库统计和恢复建议。不需要浏览器连接。

    Returns:
        总量、各状态分布、无链接候选人数、今日新增等
    """
    s = _db.stats()
    suggestions = []
    if s["by_status"].get("legacy", 0) > 0:
        suggestions.append(
            f"有 {s['by_status']['legacy']} 个 legacy 记录（仅有ID无数据），"
            "可用 boss_clear_dedup(status='legacy') 清除后重新搜索"
        )
    non_legacy = s["total"] - s["by_status"].get("legacy", 0)
    no_url = s["without_share_url"] - s["by_status"].get("legacy", 0)
    if no_url > 0 and non_legacy > 0:
        suggestions.append(
            f"有 {no_url} 个有数据的候选人没有 share_url，"
            "需要 boss_view_by_index 获取链接"
        )

    return {
        **s,
        "suggestions": suggestions,
    }


# --- Pipeline tools: filter, score, export ---

@mcp.tool()
async def boss_filter_and_score(
    top_n: int = 20,
) -> list[dict]:
    """按配置的岗位技能关键词匹配并标记 Top N，结果供人工复核。

    不使用年龄、性别等人口属性；薪资和求职状态仅按显式配置过滤。
    """
    from ranking import rank_candidates
    return rank_candidates(_db, PROFILE, top_n)


@mcp.tool()
async def boss_export_report(
    top_n: int = 10,
    include_detail: bool = True,
) -> str:
    """从数据库导出候选人报告（Markdown 格式）。

    导出 shortlisted 状态的候选人，按评分排序，生成与 260325 文档一致的格式。
    包含 Top N 表格、跟进状态表、候选人详情卡片。

    Args:
        top_n: 导出前 N 名候选人，默认 10
        include_detail: 是否包含详情卡片，默认 True

    Returns:
        Markdown 格式的候选人报告文本
    """
    from datetime import datetime

    if not 1 <= top_n <= 1000:
        raise ValueError("top_n 必须在 1 到 1000 之间")

    shortlisted = _db.query(status="shortlisted", limit=100000)
    shortlisted.sort(key=lambda x: x.get("score", 0), reverse=True)
    top = shortlisted[:top_n]

    if not top:
        return "没有 shortlisted 状态的候选人。请先运行 boss_filter_and_score。"

    today = datetime.now().strftime("%Y-%m-%d")
    job = PROFILE.get("job", {})

    lines = []
    title = " ".join(value for value in [job.get('city', ''), job.get('title', '')] if value) or "招聘"
    lines.append(f"# {title}候选人报告\n")
    lines.append(f"> 来源：BOSS 直聘搜索（{today}）")
    lines.append(f"> 岗位：{job.get('title', '未配置')} | {job.get('salary', '')} | {job.get('city', '')}")

    db_stats = _db.stats()
    lines.append(f"> 搜索总量：{db_stats['total']} 人 → 精筛 Top {len(top)}")
    lines.append("")

    # Top N table
    lines.append(f"## Top {len(top)} 候选人\n")
    lines.append("| # | 姓名 | 分数 | 年龄 | 经验 | 学历 | 公司 | 薪资 | 状态 | 院校 | BOSS链接 | 核心优势 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, c in enumerate(top):
        url = c.get("share_url", "")
        link = f"[链接]({url})" if url else "待获取"
        ft = c.get("fullText", "")[:120].replace("\n", " ").replace("|", "/")
        lines.append(
            f"| {i+1} | **{c.get('name', '?')}** | {c.get('score', '?')} "
            f"| {c.get('age', '?')} | {c.get('experience', '?')} | {c.get('education', '?')} "
            f"| {c.get('company', '?')} | {c.get('salary', '?')} | {c.get('jobStatus', '?')} "
            f"| {c.get('school', '?')} | {link} | {ft} |"
        )
    lines.append("")

    # Follow-up table
    lines.append("## 跟进状态\n")
    lines.append("| 姓名 | 分数 | BOSS链接 | 沟通 | 简历 | 一面 | 二面 | 备注 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for c in top:
        url = c.get("share_url", "")
        link = f"[链接]({url})" if url else "待获取"
        lines.append(
            f"| {c.get('name', '?')} | {c.get('score', '?')} "
            f"| {link} | - | - | - | - | |"
        )
    lines.append("")

    # Detail cards
    if include_detail:
        lines.append("---\n")
        lines.append("## 候选人详情\n")
        for i, c in enumerate(top):
            url = c.get("share_url", "")
            lines.append(f"### {i+1}. {c.get('name', '?')}（{c.get('company', '?')}）{c.get('score', '?')}分\n")
            if url:
                lines.append(f"> BOSS 链接：{url}\n")
            lines.append(
                f"**{c.get('age', '?')} | {c.get('experience', '?')} | {c.get('education', '?')} "
                f"| {c.get('jobStatus', '?')} | {c.get('salary', '?')} | {c.get('school', '?')}**\n"
            )
            ft = c.get("fullText", "")
            # Extract the description part (skip name/tags lines)
            desc_lines = [l.strip() for l in ft.split("\n") if len(l.strip()) > 20]
            desc = " ".join(desc_lines[1:4]) if len(desc_lines) > 1 else ft[:300]
            lines.append(f"{desc[:400]}\n")
            lines.append("---\n")

    return "\n".join(lines)


# --- Utility tools ---

@mcp.tool()
@browser_operation
async def boss_debug_page() -> dict:
    """调试工具：全面扫描当前页面 DOM 结构。

    Returns:
        url, title, 所有表单元素, iframe, 主要内容区块的完整信息
    """
    browser = await get_browser()
    p = browser.page
    url = p.url
    title = await p.title()

    structure = await p.evaluate("""() => {
        const body = document.body;
        if (!body) return {error: 'no body'};

        const formEls = document.querySelectorAll('input, textarea, select, [contenteditable="true"]');
        const forms = Array.from(formEls).slice(0, 20).map(el => ({
            tag: el.tagName, type: el.type || '', placeholder: el.placeholder || '',
            class: el.className?.toString().slice(0, 80) || '',
            id: el.id || '', name: el.name || '',
            visible: el.offsetParent !== null
        }));

        const iframes = Array.from(document.querySelectorAll('iframe')).slice(0, 5).map(el => ({
            src: el.src || '', id: el.id || '', class: el.className?.toString().slice(0, 80) || ''
        }));

        const topLevel = Array.from(body.children).slice(0, 15).map(el => ({
            tag: el.tagName, id: el.id || '',
            class: el.className?.toString().slice(0, 100) || '',
            childCount: el.children.length,
            text: el.innerText?.slice(0, 150) || '',
            rect: {w: el.offsetWidth, h: el.offsetHeight}
        }));

        const mainEl = document.querySelector('.main-wrap, .main, main, [class*="content"], [class*="wrap"]');
        let mainChildren = [];
        if (mainEl) {
            mainChildren = Array.from(mainEl.querySelectorAll('*')).filter(el =>
                el.children.length < 5 && el.innerText?.trim().length > 5 && el.offsetParent !== null
            ).slice(0, 40).map(el => ({
                tag: el.tagName,
                class: el.className?.toString().slice(0, 80) || '',
                text: el.innerText?.slice(0, 120) || ''
            }));
        }

        const sidebar = document.querySelector('.menu-list, .sidebar, nav');
        let contentHtml = '';
        for (const child of body.children) {
            if (child === sidebar || child.contains?.(sidebar)) continue;
            if (child.offsetWidth > 200 && child.offsetHeight > 200) {
                contentHtml = child.innerHTML.slice(0, 3000);
                break;
            }
        }

        return {
            forms, iframes, topLevel, mainChildren, contentHtml: contentHtml.slice(0, 3000)
        };
    }""")

    return {"url": url, "title": title, "structure": structure}


@mcp.tool()
@browser_operation
async def boss_reload() -> dict:
    """热重载所有模块代码，无需重启 server。

    修改 scraper.py / browser.py / evaluator.py / candidate_db.py 后调用此工具即可生效。
    保留 Chrome 窗口和标签页，下次工具调用时重新连接浏览器。

    Returns:
        重载结果
    """
    global _browser, _scraper, _evaluator, _db

    try:
        # Old browser instances keep their old class methods after importlib.reload.
        # Disconnect before replacing the class, preserving Chrome and its tabs.
        if _browser is not None:
            await _browser.close()
            _browser = None
        importlib.reload(browser_mod)
        importlib.reload(scraper_mod)
        importlib.reload(evaluator_mod)
        importlib.reload(candidate_db_mod)
        globals()["BossBrowser"] = browser_mod.BossBrowser

        if _browser is not None and _browser.is_alive:
            _scraper = scraper_mod.BossScraper(_browser)
        else:
            _scraper = None

        _evaluator = evaluator_mod.CandidateEvaluator()
        _db = candidate_db_mod.CandidateDB(CANDIDATE_DB_FILE, legacy_dedup_path=DEDUP_FILE)

        return {"status": "success", "message": "已重载 browser, scraper, evaluator, candidate_db 模块"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    if "--transport" in sys.argv and "http" in sys.argv:
        port_idx = sys.argv.index("--port") if "--port" in sys.argv else -1
        port = int(sys.argv[port_idx + 1]) if port_idx >= 0 else 8090
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run()
