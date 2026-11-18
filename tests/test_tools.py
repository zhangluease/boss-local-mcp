from pathlib import Path

from fastmcp import Client
import pytest


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result
    return result.data


@pytest.mark.asyncio
async def test_all_tools_in_local_recruiter_fixture(app):
    server, page = app["server"], app["page"]
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        assert len(tools) == 18
        assert (await call(client, "boss_login"))["status"] == "success"
        found = await call(client, "boss_search_candidates", keyword="Python", count=2)
        assert found[0]["new"] == 2
        assert len(found) == 3
        duplicates = await call(client, "boss_multi_search", keywords=["Python", "SQL"], count_per_keyword=2, auto_view=False)
        assert duplicates[0]["total_new"] == 0
        assert duplicates[0]["total_duplicates"] == 4
        viewed = await call(client, "boss_view_by_index", index=0)
        assert viewed["share_url"] == "https://zpurl.cn/fixture-candidate"
        assert Path(viewed["screenshots"][0]).is_file()
        by_id = await call(client, "boss_view_by_expect_id", expect_id="fixture-1")
        assert by_id.get("share_url") == viewed["share_url"], by_id
        assert len(await call(client, "boss_query_db", has_share_url=True)) == 2
        cached_url = await call(client, "boss_view_candidate", profile_url=viewed["share_url"])
        assert cached_url["screenshots"]
        updated = await call(client, "boss_update_candidate", expect_id="fixture-0", score=90, notes="本地测试")
        assert updated["status"] == "success"
        assert (await call(client, "boss_pipeline_status"))["total"] == 2
        ranked = await call(client, "boss_filter_and_score", top_n=2)
        assert ranked[0]["shortlisted"] == 2
        report = await call(client, "boss_export_report", top_n=2)
        assert "测试市" in report and "测试工程师" in report
        assert "北京 AI 产品经理候选人" not in report
        assert (await call(client, "boss_debug_page"))["structure"]["iframes"]
        evaluated = await call(client, "boss_evaluate_candidate", resume={"skills": ["Python", "SQL"]}, job_requirements="Python SQL")
        assert evaluated["score"] > 50
        greeted = await call(client, "boss_greet_by_index", index=0, message="仅用于本地测试")
        assert greeted["status"] == "success"
        assert await page.evaluate("fixtureGreetings") == 1
        assert await page.evaluate("fixtureMessages") == ["仅用于本地测试"]
        legacy = await call(client, "boss_view_candidate", profile_url=app["base"] + "/profile/0")
        assert "Python SQL" in legacy["full_text"]
        sent = await call(client, "boss_send_greeting", profile_url=app["base"] + "/profile/0", message="本地消息")
        assert sent["status"] == "success"
        assert await page.evaluate("fixtureMessages") == ["本地消息"]
        assert (await call(client, "boss_clear_dedup", expect_ids=["fixture-0"]))["cleared"] == 1
        assert (await call(client, "boss_clear_dedup", status="shortlisted"))["cleared"] == 1
        assert (await call(client, "boss_reload"))["status"] == "success"


@pytest.mark.asyncio
async def test_transport_does_not_enable_runtime_or_modify_console(app):
    connection = app["browser"]._browser
    commands = []
    original = connection.command

    async def record(method, *args, **kwargs):
        commands.append(method)
        return await original(method, *args, **kwargs)

    connection.command = record
    assert await app["browser"].is_logged_in()
    assert await app["page"].title() == "本地招聘测试"
    assert (await app["page"].screenshot()).startswith(b"\x89PNG")
    assert "Runtime.enable" not in commands
    assert "Debugger.enable" not in commands


@pytest.mark.asyncio
async def test_login_waits_for_backend_navigation_without_reloading(app):
    await app["page"].evaluate("""() => {
        const menu = document.querySelector('.menu-list');
        menu.style.display = 'none';
        window.fixturePageMarker = 'same-document';
        setTimeout(() => { menu.style.display = ''; }, 500);
    }""")
    async with Client(app["server"].mcp) as client:
        assert (await call(client, "boss_login"))["status"] == "success"
    assert await app["page"].evaluate("fixturePageMarker") == "same-document"


@pytest.mark.asyncio
async def test_scan_handoff_disconnects_and_reconnects_after_manual_login(app):
    from cdp import CDPConnection
    page, server = app["page"], app["server"]
    await page.goto(app["base"] + "/web/user/?intent=1")
    await page.evaluate("window.fixtureLoginMarker = 'same-login-document'")
    original_connection = app["browser"]._browser
    target_id = page.target_id
    async with Client(server.mcp) as client:
        result = await call(client, "boss_login")
        assert result["status"] == "waiting_for_login"
        assert result["browser_connection"] == "disconnected"
        assert result["next_action"] == "wait_for_user"
        assert not original_connection.is_connected()
        assert server._browser is None and server._scraper is None

        # A separate short-lived connection models the user's manual browser
        # interaction. The original tab and its document must still exist.
        observer = await CDPConnection.connect(app["endpoint"])
        try:
            target = next(t for t in await observer.pages() if t["targetId"] == target_id)
            retained = await observer.attach(target)
            assert await retained.evaluate("fixtureLoginMarker") == "same-login-document"
            await retained.goto(app["base"] + "/web/chat/index")
            await retained.wait_for_selector(".menu-list")
        finally:
            await observer.close()

        assert (await call(client, "boss_login"))["status"] == "success"
        assert server._browser.page.target_id == target_id
        assert (app["tmp"] / "cookies.json").is_file()
        found = await call(client, "boss_search_candidates", keyword="Python", count=1)
        assert found[0]["new"] == 1


@pytest.mark.asyncio
async def test_failed_login_navigation_also_releases_the_connection(app, monkeypatch):
    async def not_logged_in():
        return False

    async def fail():
        raise RuntimeError("Synthetic login navigation failure")

    connection = app["browser"]._browser
    monkeypatch.setattr(app["browser"], "is_logged_in", not_logged_in)
    monkeypatch.setattr(app["browser"], "login", fail)
    async with Client(app["server"].mcp) as client:
        with pytest.raises(Exception, match="Synthetic login navigation failure"):
            await client.call_tool("boss_login", {})
    assert not connection.is_connected()
    assert app["server"]._browser is None and app["server"]._scraper is None


@pytest.mark.asyncio
async def test_multi_search_auto_view_and_input_validation(app):
    async with Client(app["server"].mcp) as client:
        result = await call(client, "boss_multi_search", keywords=["Python"], count_per_keyword=1, auto_view=True)
        assert result[0]["total_new"] == 1
        assert (await call(client, "boss_query_db", has_share_url=True))[0]["share_url"]
        assert (await call(client, "boss_view_by_index", index=-1))["error"]
        assert (await call(client, "boss_view_by_expect_id", expect_id="missing"))["error"]
        assert (await call(client, "boss_update_candidate", expect_id="missing", status="viewed"))["status"] == "error"
        assert (await call(client, "boss_update_candidate", expect_id="fixture-0", score=101))["status"] == "error"


@pytest.mark.asyncio
async def test_view_by_id_uses_the_actual_card_and_rejects_mismatched_dialog(app):
    page, server = app["page"], app["server"]
    async with Client(server.mcp) as client:
        search = await call(client, "boss_search_candidates", keyword="Python", count=2)
        batch = search[0]["batch_id"]
        frame = server._scraper._search_frame
        await frame.evaluate("document.querySelector('a[data-contact]').removeAttribute('data-expect')")
        wrong_dialog = await page.evaluate("""() => {
            window.fixtureOpen = window.showResume;
            window.showResume = () => fixtureOpen(0);
            return true;
        }""")
        assert wrong_dialog
        failed = await call(client, "boss_view_by_expect_id", expect_id="fixture-1", batch_id=batch)
        assert failed["error_code"] == "identity_mismatch"
        assert "screenshots" not in failed
        assert server._db.get("fixture-1")["status"] == "new"
        await page.evaluate("window.showResume = window.fixtureOpen")
        result = await call(client, "boss_view_by_expect_id", expect_id="fixture-1", batch_id=batch)
        assert result["ids"]["expectId"] == "fixture-1"
        assert result["identity_check"] == "verified"
        update = await call(client, "boss_update_candidate", expect_id="fixture-1", batch_id=batch,
                            notes="人工复核：项目职责待面试确认")
        assert update["status"] == "success"
        report = await call(client, "boss_export_filter_log", batch_id=batch)
        contents = Path(report["path"]).read_text(encoding="utf-8")
        assert "fixture-1 | 本轮返回，待人工复核 |  | 已核对详情 | 是 | 2 |" in contents
        assert "人工复核：项目职责待面试确认" in contents


@pytest.mark.asyncio
async def test_filter_report_preserves_reasons_and_missing_evidence(app):
    import csv

    server, page = app["server"], app["page"]
    frame = await (await server.get_scraper())._get_search_frame(page)
    await frame.evaluate("""() => {
        document.addEventListener('keydown', event => {
            if (event.key !== 'Enter') return;
            event.stopImmediatePropagation();
            const cards = Array.from(document.querySelectorAll('li.geek-info-card a'));
            cards[0].querySelectorAll('div')[2].nextElementSibling.textContent = '深圳';
            cards[1].querySelectorAll('div')[2].nextElementSibling.textContent = '';
            document.body.insertAdjacentHTML('beforeend', '<p>没有更多了</p>');
        }, true);
    }""")
    async with Client(server.mcp) as client:
        result = await call(client, "boss_search_candidates", keyword="=Python", city="厦门", count=2, scan_mode="end")
        assert result[0]["entrypoint"] == "mcp"
        assert result[0]["stop_reason"] == "end_of_list"
        assert result[0]["new"] == 0
        assert result[0]["cards_observed"] == 2
        report = await call(client, "boss_export_filter_log", batch_id=result[0]["batch_id"], format="csv")
        with Path(report["path"]).open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        assert [row["本轮已核对详情"] for row in rows] == ["否", "否"]
        assert rows[0]["卡片阶段结果"] == "卡片条件不符"
        assert "深圳" in rows[0]["判断依据"]
        assert rows[1]["卡片阶段结果"] == "卡片证据不足，待核实"
        assert rows[0]["关键词"] == "'=Python"
        assert rows[0]["扫描停止原因"] == "页面明确提示已到底"


@pytest.mark.asyncio
@pytest.mark.parametrize("max_scrolls,reason,observed", [(1, "scroll_limit", 2), (8, "end_of_list", 4)])
async def test_scan_visits_fixed_size_virtual_rows_until_limit_or_explicit_end(app, max_scrolls, reason, observed):
    server, page = app["server"], app["page"]
    frame = await (await server.get_scraper())._get_search_frame(page)
    await frame.evaluate("""() => {
        const list = document.querySelector('#results');
        list.style = 'height:160px; overflow-y:auto';
        const end = document.createElement('p');
        document.addEventListener('keydown', event => {
            if (event.key !== 'Enter') return;
            event.stopImmediatePropagation();
            list.querySelector('a').firstChild.textContent = '新搜索结果';
        }, true);
        list.addEventListener('scroll', () => {
            if (list.scrollTop + list.clientHeight < list.scrollHeight - 2 || end.isConnected) return;
            list.querySelectorAll('a').forEach((link, index) => link.dataset.expect = 'next-' + index);
            end.textContent = '没有更多了';
            document.body.append(end);
        });
    }""")
    async with Client(server.mcp) as client:
        result = await call(client, "boss_search_candidates", keyword="Python", count=1,
                            scan_mode="end", max_scrolls=max_scrolls)
        stats = result[0]
        assert stats["stop_reason"] == reason, stats
        assert stats["unique_candidates"] == observed
        assert stats["new"] == 1
        assert result[1]["index"] == (None if observed == 4 else 0)
        report = await call(client, "boss_export_filter_log", batch_id=stats["batch_id"])
        assert report["row_count"] == observed


@pytest.mark.asyncio
async def test_end_mode_distinguishes_no_new_cards_from_explicit_end(app):
    async with Client(app["server"].mcp) as client:
        results = await call(client, "boss_search_candidates", keyword="Python", count=1, scan_mode="end")
    assert results[0]["stop_reason"] == "no_new_cards"
    assert results[0]["status"] == "partial"
    assert results[0]["unique_candidates"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("already_open", [False, True])
async def test_search_waits_for_a_slow_iframe_document(app, already_open):
    await app["page"].evaluate("""alreadyOpen => {
        window.showSearch = () => {
            document.getElementById('searchContent').innerHTML = '<iframe src="/search?delay=0.5"></iframe>';
        };
        if (alreadyOpen) showSearch();
    }""", already_open)
    async with Client(app["server"].mcp) as client:
        result = await call(client, "boss_multi_search", keywords=["Python"], count_per_keyword=1, auto_view=True)
        assert result[0]["total_new"] == 1, result
        assert (await call(client, "boss_query_db", has_share_url=True))[0]["share_url"]


@pytest.mark.asyncio
async def test_failed_greeting_does_not_mark_candidate_greeted(app, monkeypatch):
    app["server"]._db.add({"expectId": "fixture-0"})
    from scraper import BossScraper

    async def fail(*args, **kwargs):
        return {"status": "error", "message": "测试错误", "ids": {"expectId": "fixture-0"}}

    monkeypatch.setattr(BossScraper, "greet_by_index", fail)
    async with Client(app["server"].mcp) as client:
        assert (await call(client, "boss_greet_by_index", index=0))["status"] == "error"
    assert app["server"]._db.get("fixture-0")["status"] == "new"


@pytest.mark.asyncio
async def test_overlay_does_not_redirect_a_click_to_another_control(app):
    from scraper import BossScraper
    from cdp import CDPError
    frame = await BossScraper(app["browser"])._get_search_frame(app["page"])
    element = await frame.query_selector("input.search-input")
    await app["page"].evaluate("""() => {const overlay=document.createElement('div');overlay.style='position:fixed;inset:0;z-index:999;background:white';document.body.appendChild(overlay);} """)
    with pytest.raises(CDPError, match="covered"):
        await element.click()
    assert await app["page"].evaluate("fixtureGreetings") == 0


@pytest.mark.asyncio
async def test_browser_tool_calls_are_serialized(app):
    import asyncio
    async with Client(app["server"].mcp) as client:
        results = await asyncio.gather(call(client, "boss_search_candidates", keyword="Python", count=2), call(client, "boss_search_candidates", keyword="SQL", count=2))
        assert sorted(result[0]["new"] for result in results) == [0, 2]


@pytest.mark.asyncio
async def test_local_ocr_is_available_on_resume_view(app):
    from ocr import available_engine
    if available_engine() == "unavailable":
        pytest.skip("No local OCR engine installed")
    async with Client(app["server"].mcp) as client:
        await call(client, "boss_search_candidates", keyword="Python", count=1)
        result = await call(client, "boss_view_by_index", index=0, include_ocr=True)
        assert "Python" in result.get("ocr_text", ""), result.get("ocr_error")


@pytest.mark.asyncio
async def test_missing_custom_message_input_is_not_reported_as_sent(app, monkeypatch):
    original = app["page"].query_selector

    async def query(selector):
        if "textarea" in selector:
            return None
        return await original(selector)

    monkeypatch.setattr(app["page"], "query_selector", query)
    async with Client(app["server"].mcp) as client:
        result = await call(client, "boss_send_greeting", profile_url=app["base"] + "/profile/0", message="本地测试")
        assert result["status"] == "partial"
        assert result["greeting_sent"] is True
        assert await app["page"].evaluate("fixtureMessages") == []
