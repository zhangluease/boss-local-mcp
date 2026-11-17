"""Connect to the user's dedicated Chrome without subscribing to console events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from cdp import CDPConnection
from browser_install import BrowserVersionError, configured_executable, executable_version, require_version
from config import BOSS_BASE_URL, CHROME_PROFILE_DIR, COOKIES_FILE, MAX_DELAY, MIN_DELAY

log = logging.getLogger("boss-local-browser")
CDP_URL = os.getenv("BOSS_CDP_URL", "http://localhost:9222")


def is_boss_url(url):
    hostname = urlsplit(url).hostname or ""
    return hostname == "zhipin.com" or hostname.endswith(".zhipin.com")


class BossBrowser:
    def __init__(self):
        self._browser = None
        self._page = None

    async def launch(self):
        if not await self._try_cdp_connect(CDP_URL):
            await self._launch_system_chrome()
            for _ in range(30):
                if await self._try_cdp_connect(CDP_URL):
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("无法连接 Chrome。请运行 boss-local-mcp browser 后重新检查。")

    async def _try_cdp_connect(self, url):
        connection = None
        try:
            connection = await CDPConnection.connect(url)
            require_version(connection.browser_version)
            pages = await connection.pages()
            boss_pages = [page for page in pages if is_boss_url(page.get("url", ""))]
            backend = [page for page in boss_pages if urlsplit(page["url"]).path.startswith(("/web/boss/", "/web/chat/"))]
            if boss_pages:
                target = (backend or boss_pages)[-1]
            else:
                created = await connection.command("Target.createTarget", {"url": "about:blank"})
                target = {"targetId": created["targetId"], "url": "about:blank"}
            self._page = await connection.attach(target)
            self._browser = connection
            return True
        except BrowserVersionError:
            if connection:
                await connection.close()
            raise
        except Exception as error:
            if connection:
                await connection.close()
            log.debug("Chrome connection failed: %s", error)
            return False

    async def _launch_system_chrome(self):
        executable = configured_executable()
        require_version(executable_version(executable))
        port = urlsplit(CDP_URL).port or 9222
        Path(CHROME_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        subprocess.Popen([
            str(executable), f"--remote-debugging-port={port}",
            f"--user-data-dir={CHROME_PROFILE_DIR}", "--no-first-run", "--no-default-browser-check",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)

    @property
    def page(self):
        if not self._page:
            raise RuntimeError("浏览器尚未连接")
        return self._page

    @property
    def is_alive(self):
        return bool(self._browser and self._browser.is_connected() and self._page and not self._page.is_closed())

    async def close(self):
        if self._browser:
            await self._browser.close()
        self._browser = None
        self._page = None

    async def _save_cookies(self):
        if not self._browser:
            return
        result = await self._browser.command("Network.getCookies", {"urls": [BOSS_BASE_URL]}, self.page.session)
        cookies = [cookie for cookie in result.get("cookies", [])
                   if cookie.get("domain", "").lstrip(".") in ("zhipin.com", "www.zhipin.com")]
        path = Path(COOKIES_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(cookies, output, ensure_ascii=False)
        path.chmod(0o600)

    async def is_logged_in(self):
        if not self.is_alive or not is_boss_url(self.page.url):
            return False
        inspection = """() => {
            const visible = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e); return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden'); };
            return {
                path:location.pathname,
                login:Array.from(document.querySelectorAll('.header-login-btn,.link-signin')).some(visible),
                nav:Array.from(document.querySelectorAll('.user-nav,.btn-post-job,.nav-figure,.menu-list')).some(visible),
                workspace:['推荐牛人','职位管理','推荐人才'].some(label=>(document.body.innerText||'').includes(label))
            };
        }"""
        # A restored/background tab can briefly have a backend URL but no SPA
        # navigation yet. Inspect its DOM without refreshing or navigating.
        deadline = time.monotonic() + 5
        while True:
            state = await self.page.evaluate(inspection)
            if state["login"] or state["path"].startswith("/web/user"):
                return False
            backend = state["path"].startswith(("/web/boss/", "/web/chat/"))
            if state["nav"] or (backend and state["workspace"]):
                return True
            if not backend or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _check_current_page_logged_in(self):
        return await self.is_logged_in()

    async def login(self):
        if not is_boss_url(self.page.url):
            await self.page.goto(f"{BOSS_BASE_URL}/web/user/?intent=1")
        await self.page.bring_to_front()
        return {"status": "waiting_for_login", "message": "请在 Chrome 中手动扫码登录招聘者账号，完成后再次调用 boss_login；不会自动刷新或重复跳转。"}

    async def check_and_screenshot_verification(self):
        verification = await self.page.evaluate("""() => {
            const text=document.body?.innerText||'';
            return ['安全验证','滑动验证','请完成验证'].some(value=>text.includes(value)) ||
                Array.from(document.querySelectorAll('.verify-wrap,.captcha,.slider-verify')).some(e=>e.offsetWidth>0&&e.offsetHeight>0);
        }""")
        if not verification:
            return None
        from config import DATA_DIR
        screenshot = str(Path(DATA_DIR) / "verification.png")
        await self.page.screenshot(path=screenshot)
        return {"needs_verification": True, "screenshot": screenshot, "message": "请在浏览器中手动完成安全验证，再重新调用工具。"}

    async def random_delay(self):
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
