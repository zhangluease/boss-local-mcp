import asyncio
import base64
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
import qrcode

os.environ.setdefault("BOSS_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data" / "test-runtime"))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_site():
    buffer = io.BytesIO()
    qrcode.make("https://zpurl.cn/fixture-candidate").save(buffer, format="PNG")
    qr = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/"):
                body = json.dumps({"zpData": {"geekList": [{"expectId": f"fixture-{i}", "salaryDesc": "20-30K", "encryptGeekId": f"geek-{i}"} for i in range(2)]}}).encode()
                mime = "application/json"
            elif self.path.startswith("/search"):
                body = (FIXTURES / "search.html").read_bytes()
                mime = "text/html; charset=utf-8"
            elif self.path.startswith("/c-resume"):
                body = b'''<!doctype html><canvas width="900" height="600" style="width:900px;height:600px"></canvas><script>const c=document.querySelector('canvas');const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,900,600);x.fillStyle='black';x.font='25px sans-serif';x.fillText('Synthetic resume: Python SQL',40,80);c.onclick=()=>parent.showShare();</script>'''
                mime = "text/html; charset=utf-8"
            elif self.path.startswith("/profile/"):
                body = '''<!doctype html><title>测试简历</title><p>Python SQL 项目经验</p><button class="btn-greet" onclick="contact(this)">打招呼</button><script>window.fixtureGreetings=0;window.fixtureMessages=[];function contact(button){fixtureGreetings++;button.insertAdjacentHTML('afterend','<textarea class="chat-input"></textarea><button class="btn-send" onclick="fixtureMessages.push(this.previousElementSibling.value)">发送</button>')}</script>'''.encode()
                mime = "text/html; charset=utf-8"
            else:
                body = (FIXTURES / "app.html").read_text().replace("__QR_DATA__", qr).encode()
                mime = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}"
    http.shutdown()
    http.server_close()


@pytest.fixture(scope="session")
def chrome_endpoint(tmp_path_factory):
    executable = os.getenv("BOSS_TEST_CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
    if not executable and Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file():
        executable = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not executable:
        pytest.skip("Headless Chrome is not installed")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    profile = tmp_path_factory.mktemp("headless-chrome")
    arguments = [executable, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "about:blank"]
    if os.getenv("BOSS_TEST_NO_SANDBOX") == "1":
        arguments.insert(1, "--no-sandbox")
    log_path = profile / "chrome.log"
    browser_log = log_path.open("w")
    process = subprocess.Popen(arguments, stdout=browser_log, stderr=browser_log)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(endpoint + "/json/version", timeout=0.2):
                    break
            except Exception:
                if process.poll() is not None:
                    pytest.fail("Headless Chrome exited during startup:\n" + log_path.read_text(errors="replace")[-2000:])
                time.sleep(0.1)
        else:
            pytest.fail("Headless Chrome did not start")
        yield endpoint
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        browser_log.close()


@pytest_asyncio.fixture
async def app(chrome_endpoint, fixture_site, tmp_path, monkeypatch):
    import browser as browser_module
    import config
    import scraper as scraper_module
    import server
    from browser import BossBrowser
    from candidate_db import CandidateDB
    from cdp import CDPConnection

    connection = await CDPConnection.connect(chrome_endpoint)
    target = await connection.command("Target.createTarget", {"url": fixture_site + "/web/chat/index"})
    page = await connection.attach({"targetId": target["targetId"], "url": fixture_site + "/web/chat/index"})
    await page.wait_for_selector(".menu-list")
    browser = BossBrowser()
    browser._browser, browser._page = connection, page
    monkeypatch.setattr(browser_module, "is_boss_url", lambda url: url.startswith(fixture_site))
    monkeypatch.setattr(browser_module, "BOSS_BASE_URL", fixture_site)
    monkeypatch.setattr(browser_module, "COOKIES_FILE", str(tmp_path / "cookies.json"))
    monkeypatch.setattr(config, "BOSS_SEARCH_URL", fixture_site + "/web/chat/search")
    monkeypatch.setattr(scraper_module, "BOSS_BASE_URL", fixture_site)
    monkeypatch.setattr(scraper_module, "ACTION_WAIT_SCALE", 0.02)
    monkeypatch.setattr(scraper_module, "SCREENSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(browser_module, "MIN_DELAY", 0)
    monkeypatch.setattr(browser_module, "MAX_DELAY", 0)
    monkeypatch.setattr(server, "_browser", browser)
    monkeypatch.setattr(server, "_scraper", None)
    monkeypatch.setattr(server, "_db", CandidateDB(str(tmp_path / "candidates.json")))
    monkeypatch.setattr(server, "PROFILE", {"job": {"title": "测试工程师", "city": "测试市"}, "keywords": ["Python", "SQL"], "scoring": {"tech_keywords": ["Python", "SQL"]}})
    yield {"browser": browser, "page": page, "server": server, "base": fixture_site, "tmp": tmp_path, "endpoint": chrome_endpoint}
    cleanup = connection if connection.is_connected() else await CDPConnection.connect(chrome_endpoint)
    try:
        await cleanup.command("Target.closeTarget", {"targetId": target["targetId"]})
    finally:
        await cleanup.close()
