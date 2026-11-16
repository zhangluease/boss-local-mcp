from pathlib import Path

from fastmcp import Client
import pytest


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result
    return result.data


@pytest.mark.asyncio
async def test_all_seventeen_tools_in_local_recruiter_fixture(app):
    server, page = app["server"], app["page"]
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        assert len(tools) == 17
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
