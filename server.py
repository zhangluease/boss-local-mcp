"""BOSS 直聘招聘者 MCP Server — 候选人筛选与评估."""

import sys
import os
import importlib
import asyncio
from collections import Counter
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Literal
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
import browser as browser_mod
import scraper as scraper_mod
import evaluator as evaluator_mod
import candidate_db as candidate_db_mod
from browser import BossBrowser
from scraper import BossScraper
from evaluator import CandidateEvaluator
from candidate_db import CandidateDB
from config import DATA_DIR, DEDUP_FILE, CANDIDATE_DB_FILE, PROFILE
from filters import card_filter
from run_history import RunHistory, new_id, record_detail_attempt, timestamp

mcp = FastMCP("boss-local-mcp")

# Shared instances
_browser: BossBrowser | None = None
_scraper: BossScraper | None = None
_evaluator = CandidateEvaluator()

# --- Candidate database (replaces bare-ID dedup set) ---

_db = CandidateDB(CANDIDATE_DB_FILE, legacy_dedup_path=DEDUP_FILE)
_browser_lock = asyncio.Lock()


_history = RunHistory(DATA_DIR)
_invocation = ContextVar("mcp_invocation", default=None)


class InvocationHistory(Middleware):
    async def on_call_tool(self, context, call_next):
        if context.source != "client":
            return await call_next(context)
        call = {"call_id": new_id(), "batch_id": None}
        _history.start_call(call["call_id"], context.message.name)
        token = _invocation.set(call)
        outcome = "failed"
        try:
            result = await call_next(context)
            outcome = "returned"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            _invocation.reset(token)
            _history.finish_call(call["call_id"], outcome, call["batch_id"])


mcp.add_middleware(InvocationHistory())


def _entrypoint():
    return "mcp" if _invocation.get() else "direct"


def _link_batch(batch_id):
    invocation = _invocation.get()
    if invocation is not None:
        invocation["batch_id"] = batch_id


def _start_batch(tool, auto_view):
    batch = {
        "batch_id": new_id(), "source": "keyword_search", "entrypoint": _entrypoint(),
        "tool": tool, "started_at": timestamp(), "status": "running", "auto_view": auto_view,
        "scans": [], "rows": [],
    }
    _link_batch(batch["batch_id"])
    _history.save_batch(batch)
    return batch


def _detail_outcome(result):
    identity = result.get("identity_check", "")
    code = result.get("error_code", "")
    if not result.get("error") and identity == "verified" and result.get("screenshots"):
        return "opened", True, identity, ""
    if code == "not_visible":
        return "not_visible", False, identity, code
    if code in ("identity_mismatch", "identity_unverified") or identity == "card_only":
        return "identity_unverified", False, identity, code
    return "failed", False, identity, code or "view_error"


def _save_verified_detail(result):
    if not result.get("error") and result.get("identity_check") == "verified":
        expect_id = result["ids"]["expectId"]
        candidate = _db.get(expect_id)
        if candidate is None:
            return
        fields = {}
        if candidate["status"] == "new":
            fields["status"] = "viewed"
        if result.get("share_url"):
            fields["share_url"] = result["share_url"]
        if fields:
            _db.update(expect_id, **fields)


async def _search_keyword(batch, keyword, city, experience, salary, count, scan_mode, max_scrolls):
    filters = {"city": city, "experience": experience, "salary": salary}
    scan = {
        "order": len(batch["scans"]) + 1, "keyword": keyword, "filters": filters,
        "scan_mode": scan_mode, "target_count": count,
        "scroll_count": 0, "stop_reason": "search_error", "cards_observed": 0,
        "unique_candidates": 0, "fetched": 0, "new": 0, "duplicates": 0,
    }
    batch["scans"].append(scan)
    selected = []
    verification = None
    decisions = Counter()
    try:
        scraper = await get_scraper()
        result = await scraper.scan_candidates(keyword, city, experience, salary, count, scan_mode, max_scrolls)
        verification = result["verification"]
        scan.update(scroll_count=result["scroll_count"], stop_reason=result["stop_reason"],
                    cards_observed=len(result["rows"]))
        if result.get("error"):
            scan["error"] = result["error"]
        for ordinal, observation in enumerate(result["rows"], 1):
            card, reasons = observation["candidate"], observation["reasons"]
            expect_id = card["expectId"]
            if expect_id:
                scan["unique_candidates"] += 1
            if not expect_id:
                disposition = "missing_id"
            elif reasons:
                disposition = "mismatch" if any(reason["result"] == "mismatch" for reason in reasons) else "insufficient_evidence"
            elif scan["fetched"] >= count:
                disposition = "result_limit"
            else:
                scan["fetched"] += 1
                if _db.add(card, source_keyword=keyword):
                    disposition = "selected"
                    scan["new"] += 1
                else:
                    disposition = "duplicate"
                    scan["duplicates"] += 1
            decisions[disposition] += 1
            row = {
                "row_id": f"{scan['order']}.{ordinal}", "scan_order": scan["order"], "keyword": keyword,
                "expect_id": expect_id, "disposition": disposition, "reasons": reasons,
                "card_evidence": {field: card.get(field) or "" for field in ("expectCity", "experience", "salary")},
                "detail_opened": False, "detail_attempts": [],
                "detail_status": "not_attempted" if batch["auto_view"] and disposition == "selected" else "not_requested",
            }
            batch["rows"].append(row)
            if disposition == "selected":
                if batch["tool"] == "boss_multi_search":
                    card["_source_keyword"] = keyword
                selected.append((card, row))

        if selected:
            _db._save()
        if batch["auto_view"]:
            scan.update(viewed=0, view_failed=0, share_links=0)
            _history.save_batch(batch)
            for card, row in selected:
                if verification:
                    break
                try:
                    detail = await scraper.view_candidate_by_expect_id(card["expectId"])
                except Exception:
                    detail = {"error": "读取简历失败", "error_code": "view_error"}
                outcome = _detail_outcome(detail)
                record_detail_attempt(row, *outcome)
                if outcome[1]:
                    scan["viewed"] += 1
                    _save_verified_detail(detail)
                    if detail.get("share_url"):
                        card["share_url"] = detail["share_url"]
                        scan["share_links"] += 1
                else:
                    scan["view_failed"] += 1
                    verification = await scraper.browser.check_and_screenshot_verification()
                    if verification:
                        scan["view_stop_reason"] = "verification"
    except Exception as error:
        scan["stop_reason"] = "search_error"
        scan["error"] = str(error)
    finally:
        scan["card_outcomes"] = dict(decisions)
        _history.save_batch(batch)
    return scan, [card for card, _ in selected], verification


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
    scan_mode: Literal["target", "end"] = "target",
    max_scrolls: int = 40,
) -> list[dict]:
    """关键词搜索。返回批次 ID、卡片统计和本轮新增候选人。

    target 模式达到 count 个可通过卡片过滤的候选人后停止。end 模式继续扫描当前列表，
    直到页面明确结束、无新增、超时或达到 max_scrolls。count 始终限制返回数量。
    experience 衡量卡片总工作年限。通过 boss_export_filter_log 导出本轮过滤记录。
    """
    return await _run_search("boss_search_candidates", [keyword], city, experience, salary,
                             count, scan_mode, max_scrolls, auto_view=False)


@mcp.tool()
@browser_operation
async def boss_multi_search(
    keywords: list[str] | None = None,
    city: str = "",
    experience: str = "",
    count_per_keyword: int = 50,
    auto_view: bool = True,
    scan_mode: Literal["target", "end"] = "target",
    max_scrolls: int = 40,
    salary: str = "",
) -> list[dict]:
    """按关键词顺序搜索，跨关键词去重，返回同一批次 ID 和每个关键词的停止原因。

    auto_view=True 时，按候选人 ID 查看本轮新增卡片并核对简历身份。
    如只需卡片预筛，传入 auto_view=False。用 boss_export_filter_log 导出过滤记录。
    """
    if keywords is None:
        keywords = PROFILE.get("keywords", [])
    if not city:
        city = PROFILE.get("job", {}).get("city", "")
    return await _run_search("boss_multi_search", keywords, city, experience, salary,
                             count_per_keyword, scan_mode, max_scrolls, auto_view)


async def _run_search(tool, keywords, city, experience, salary, count, scan_mode, max_scrolls, auto_view):
    if not keywords or not all(isinstance(keyword, str) and keyword.strip() for keyword in keywords):
        raise ValueError("请提供非空关键词，或在 search_profile.yaml 中配置关键词")
    if not 1 <= count <= 300 or not 1 <= max_scrolls <= 200:
        raise ValueError("目标数量须在 1 到 300 之间，max_scrolls 须在 1 到 200 之间")
    card_filter(city, experience, salary)

    batch = _start_batch(tool, auto_view)
    initial_total = _db.stats()["total"]
    candidates = []
    verification = None
    batch["status"] = "interrupted"
    try:
        for keyword in keywords:
            _, found, verification = await _search_keyword(
                batch, keyword, city, experience, salary, count, scan_mode, max_scrolls
            )
            candidates.extend(found)
            if verification:
                break
        complete = {"target_reached", "end_of_list", "empty_results"}
        all_scans_finished = all(scan["stop_reason"] in complete for scan in batch["scans"])
        has_view_failure = any(scan.get("view_failed") for scan in batch["scans"])
        batch["status"] = "complete" if all_scans_finished and not has_view_failure else "partial"
    finally:
        batch["finished_at"] = timestamp()
        _history.save_batch(batch)

    scans = batch["scans"]
    meta = {
        "_stats": True, "batch_id": batch["batch_id"], "entrypoint": batch["entrypoint"],
        "source": batch["source"], "status": batch["status"], "cumulative_seen": _db.stats()["total"],
    }
    if verification:
        meta.update(verification, _partial=True, completed_keywords=len(scans) - 1)
    if tool == "boss_search_candidates":
        scan = scans[0]
        meta.update(scan, total_fetched=scan["fetched"],
                    filter_scope="loaded_candidates" if city or experience or salary else "none")
        if scan["stop_reason"] in ("search_error", "search_timeout") and not scan["cards_observed"]:
            meta["error"] = scan.get("error") or "等待新搜索结果超时"
        return [meta, *candidates]

    per_keyword = [{"_keyword_stats": True, **scan} for scan in scans]
    meta.update({
        "_multi_search": True, "keywords_count": len(keywords),
        "total_new": len(candidates), "total_fetched": sum(scan["fetched"] for scan in scans),
        "total_duplicates": sum(scan["duplicates"] for scan in scans),
        "new_this_session": _db.stats()["total"] - initial_total,
        "errors": [{"keyword": scan["keyword"], "message": scan.get("error") or "等待新搜索结果超时"}
                   for scan in scans if scan["stop_reason"] in ("search_error", "search_timeout")],
    })
    if auto_view:
        meta["total_viewed"] = sum(scan.get("viewed", 0) for scan in scans)
        meta["total_view_failed"] = sum(scan.get("view_failed", 0) for scan in scans)
    return [meta, *per_keyword, *candidates]


@mcp.tool()
async def boss_export_filter_log(batch_id: str, format: Literal["md", "csv"] = "md") -> dict:
    """导出本次关键词搜索中实际读到的卡片和详情查看记录。返回本地文件路径。"""
    report = _history.export(batch_id, format)
    _link_batch(batch_id)
    return report


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
                result = await scraper.view_candidate_by_expect_id(candidate["expectId"])
                _save_verified_detail(result)
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

    _save_verified_detail(result)
    return await add_local_ocr(result) if include_ocr and not result.get("error") else result


@mcp.tool()
@browser_operation
async def boss_view_by_expect_id(expect_id: str, include_ocr: bool = False, batch_id: str = "") -> dict:
    """按当前列表卡片的 expectId 打开并核对简历，可选 OCR。

    使用搜索返回的 batch_id 可将此次查看结果写入该批次的过滤报告。
    """
    expect_id = expect_id.strip()
    if batch_id:
        if not _history.candidate_in_batch(batch_id, expect_id):
            return {"error": "该候选人 ID 不在指定批次中"}
        _link_batch(batch_id)
    try:
        scraper = await get_scraper()
        result = await scraper.view_candidate_by_expect_id(expect_id)
    except Exception:
        if batch_id:
            _history.record_detail(batch_id, expect_id, "failed", False, code="view_error")
        raise

    if batch_id:
        _history.record_detail(batch_id, expect_id, *_detail_outcome(result))
    _save_verified_detail(result)
    return await add_local_ocr(result) if include_ocr and not result.get("error") else result


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
    batch_id: str = "",
) -> dict:
    """更新候选人状态、评分或备注。

    Claude 在对话中评估候选人后，调用此工具将评分结果写回数据库。

    Args:
        expect_id: 候选人 expectId
        status: 新状态（new/viewed/shortlisted/greeted/rejected）
        score: 匹配度评分（0-100）
        notes: 备注信息
        share_url: 分享链接
        batch_id: 可选。将显式提供的状态、分数或备注写入指定批次的复核记录。

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

    if batch_id and not _history.candidate_in_batch(batch_id, expect_id):
        return {"status": "error", "message": "该候选人 ID 不在指定批次中"}
    if _db.update(expect_id, **fields):
        if batch_id:
            _link_batch(batch_id)
            if status or score is not None or notes:
                _history.record_follow_up(batch_id, expect_id, status, score, notes)
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
        "entrypoint": _entrypoint(),
        "call_id": _invocation.get()["call_id"] if _invocation.get() else None,
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
