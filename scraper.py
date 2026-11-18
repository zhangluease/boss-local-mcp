"""BOSS 直聘 page scraper — works with SPA iframe-based search UI."""

import asyncio
import base64
import functools
import logging
import os
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from browser import BossBrowser
from config import BOSS_BASE_URL
from filters import card_filter

from qr import HAS_QR_DECODER, decode_image

log = logging.getLogger("boss-scraper")

SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "boss-recruiter-screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.chmod(SCREENSHOT_DIR, 0o700)
ACTION_WAIT_SCALE = 1.0


async def _wait(seconds):
    await asyncio.sleep(seconds * ACTION_WAIT_SCALE)


# --- Retry decorator for resilient operations ---

def retry(max_attempts=3, delay=1.0):
    """Retry async methods with automatic dialog cleanup between attempts."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    last_error = e
                    log.warning(f"{func.__name__} attempt {attempt+1} failed: {e}")
                    if attempt < max_attempts - 1:
                        await self._cleanup_dialogs()
                        await _wait(delay)
            raise last_error
        return wrapper
    return decorator

CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100",
    "深圳": "101280600", "杭州": "101210100", "成都": "101270100",
    "南京": "101190100", "武汉": "101200100", "西安": "101110100",
    "苏州": "101190400", "天津": "101030100", "重庆": "101040100",
    "长沙": "101250100", "郑州": "101180100", "东莞": "101281600",
    "青岛": "101120200", "合肥": "101220100", "佛山": "101280800",
    "宁波": "101210400", "厦门": "101230200", "大连": "101070200",
    "福州": "101230100", "济南": "101120100", "珠海": "101280700",
    "无锡": "101190200", "昆明": "101290100", "哈尔滨": "101050100",
    "沈阳": "101070100", "长春": "101060100", "石家庄": "101090100",
    "太原": "101100100", "南宁": "101300100", "贵阳": "101260100",
    "兰州": "101160100", "海口": "101310100",
}

# JS to extract candidate info from cards (reused in search_candidates)
EXTRACT_CARDS_JS = """() => {
    const cards = document.querySelectorAll("li.geek-info-card");
    const results = [];

    for (let idx = 0; idx < cards.length; idx++) {
        const card = cards[idx];
        const text = card.innerText || "";
        const link = card.querySelector("a[data-contact]");
        const lines = text.split("\\n").map(l => l.trim()).filter(l => l);

        const name = lines[0] || "未知";

        let age = "", experience = "", education = "", jobStatus = "", salary = "";
        for (const line of lines) {
            const parts = line.split(/\\s{2,}/);
            if (parts.length >= 3) {
                for (const part of parts) {
                    if (part.includes("岁")) age = part;
                    else if (part.includes("年")) experience = part;
                    else if (["本科", "硕士", "博士", "大专", "高中"].some(e => part.includes(e))) education = part;
                    else if (part.includes("离职") || part.includes("在职") || part.includes("到岗")) jobStatus = part;
                    else if (part.includes("K") || part.includes("k") || part.includes("面议") || part.includes("薪")) salary = part;
                }
                if (age || experience) break;
            }
        }

        if (!age) age = text.match(/\\d{1,2}岁/)?.[0] || '';
        if (!experience) experience = text.match(/\\d+(?:-\\d+)?年(?:以上|以内)?|应届|经验不限/)?.[0] || '';
        if (!education) education = lines.find(line=>['博士','硕士','本科','大专','高中'].some(value=>line===value)) || '';
        if (!salary) salary = text.match(/\\d+(?:\\.\\d+)?(?:[-~]\\d+(?:\\.\\d+)?)?[Kk](?:·\\d+薪)?|薪资面议/)?.[0] || '';
        if (!jobStatus) jobStatus = lines.find(line=>/离职|在职|到岗|暂不考虑/.test(line)) || '';
        const skillEls = card.querySelectorAll(".rcd-tags span, .tag-item, [class*='tag']");
        const skills = Array.from(skillEls).map(el => el.innerText.trim()).filter(s => s.length > 0 && s.length < 20);

        const cityIdx = lines.findIndex(l => l === "期望城市" || l === "期望");
        const cityText = cityIdx < 0 ? "" : (lines[cityIdx + 1] || "");
        const expectCity = ["期望城市", "期望", "职位", "院校"].includes(cityText) ? "" : cityText;

        let company = "", title = "";
        const posIdx = lines.findIndex(l => l === "职位");
        if (posIdx >= 0) {
            company = lines[posIdx + 1] || "";
            title = lines[posIdx + 2] || "";
        }

        let school = "", major = "";
        const eduIdx = lines.findIndex(l => l === "院校");
        if (eduIdx >= 0) {
            school = lines[eduIdx + 1] || "";
            major = lines[eduIdx + 2] || "";
        }

        const expectId = link ? link.getAttribute("data-expect") : "";
        const lid = link ? link.getAttribute("data-lid") : "";
        const jid = link ? link.getAttribute("data-jid") : "";

        results.push({
            index: idx, name, age, experience, education, jobStatus, salary,
            skills: skills.slice(0, 8),
            expectCity, company, title, school, major,
            expectId, lid, jid,
            fullText: text.slice(0, 500)
        });
    }
    return results;
}"""


SEARCH_SNAPSHOT_JS = """() => {
    const markers = Array.from(document.querySelectorAll('p, span, div')).filter(el =>
        !el.childElementCount && !el.closest('li.geek-info-card') && el.getClientRects().length &&
        getComputedStyle(el).visibility === 'visible');
    const labels = markers.map(el => el.innerText.trim());
    return {
        cards: (""" + EXTRACT_CARDS_JS + """)(),
        end: labels.some(line => /^(没有更多(?:了|人才|候选人|数据)?|已(?:经)?到底了|到底了|已加载全部)(?:[~～。!！])?$/.test(line)),
        empty: labels.some(line => /^(暂无(?:相关)?(?:结果|搜索结果|候选人|人才)|未找到(?:相关)?(?:候选人|人才))$/.test(line))
    };
}"""

SCROLL_SEARCH_JS = """reset => {
    const card = document.querySelector('li.geek-info-card');
    let container = card?.parentElement;
    while (container && container !== document.scrollingElement) {
        const overflow = getComputedStyle(container).overflowY;
        if (/auto|scroll|overlay/.test(overflow) && container.scrollHeight > container.clientHeight) break;
        container = container.parentElement;
    }
    container ||= document.scrollingElement;
    if (!container) return true;
    if (reset) {
        const moved = container.scrollTop > 0;
        container.scrollTop = 0;
        return moved;
    }
    const next = container.scrollTop + Math.max(1, Math.floor(container.clientHeight * 0.85));
    const last = container.scrollHeight - container.clientHeight;
    container.scrollTop = Math.min(next, last);
    return next >= last - 2;
}"""


def _enrich_card(card, api_cards):
    candidate = card.copy()
    api = api_cards.get(candidate["expectId"], {})
    if not candidate["salary"] and api.get("salaryDesc"):
        candidate["salary"] = api["salaryDesc"]
    if api.get("encryptGeekId"):
        candidate["geekId"] = api["encryptGeekId"]
    return candidate


class BossScraper:
    """Scrapes candidate data from BOSS 直聘 recruiter SPA (iframe-based)."""

    def __init__(self, browser: BossBrowser):
        self.browser = browser
        self._search_frame = None  # cache the search iframe

    async def get_visible_expect_ids(self) -> list[str]:
        """Return all expectIds currently visible on the search page."""
        frame = self._search_frame
        if not frame:
            return []
        try:
            return await frame.evaluate("""() => {
                const links = document.querySelectorAll("li.geek-info-card a[data-contact]");
                const ids = [];
                for (const link of links) {
                    const eid = link.getAttribute('data-expect');
                    if (eid) ids.push(eid);
                }
                return ids;
            }""")
        except Exception:
            return []

    async def _cleanup_dialogs(self):
        """Dismiss normal resume dialogs through their own close controls."""
        p = self.browser.page
        await p.bring_to_front()
        await p.evaluate("""() => {
            const selectors = ['.share-dialog-close','.boss-dialog__close','.boss-dialog__header .close','.boss-dialog__body .close-btn','.dialog-wrap .close','.boss-popup__close'];
            selectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(button => {
                    if (button.offsetWidth && button.offsetHeight && !button.closest('.v-leave-active')) button.click();
                });
            });
        }""")
        for _ in range(30):
            remaining = await p.evaluate("Array.from(document.querySelectorAll('.dialog-lib-resume,.c-share-box,.boss-dialog__body')).some(e=>e.offsetWidth&&e.offsetHeight)")
            if not remaining:
                return
            await _wait(0.1)

    async def _find_share_icon_position(self, resume_frame) -> tuple[int, int] | None:
        """Dynamically calculate the forward icon position on canvas."""
        canvas_size = await resume_frame.evaluate("""() => {
            const c = document.querySelector('canvas');
            return c ? {w: c.offsetWidth, h: c.offsetHeight} : null;
        }""")
        if not canvas_size:
            return None
        # Forward icon is at ~94% width, ~9.5% height of canvas
        x = int(canvas_size['w'] * 0.938)
        y = int(canvas_size['h'] * 0.095)
        return (x, y)

    @retry(max_attempts=3, delay=1.0)
    async def _get_search_frame(self, p):
        """Navigate to search page and return the search iframe's Frame object."""
        existing = await p.query_selector("#searchContent iframe")
        if existing:
            frame = await existing.content_frame()
            if frame:
                await frame.wait_for_selector("input.search-input", timeout=10000)
                self._search_frame = frame
                return frame
        search_menu = await p.query_selector("dl.menu-geeksearch")
        if not search_menu:
            # Not on recruiter SPA — navigate there first
            from config import BOSS_SEARCH_URL
            log.info(f"Navigating to recruiter search page: {BOSS_SEARCH_URL}")
            await p.goto(BOSS_SEARCH_URL, wait_until="domcontentloaded")
            search_menu = await p.wait_for_selector("dl.menu-geeksearch", timeout=10000)

        if search_menu:
            await search_menu.click()

        iframe_el = await p.wait_for_selector("#searchContent iframe", timeout=10000)
        frame = await iframe_el.content_frame()
        if not frame:
            raise RuntimeError("搜索 iframe 尚未就绪")
        await frame.wait_for_selector("input.search-input", timeout=10000)
        self._search_frame = frame
        return frame

    async def search_candidates(
        self,
        keyword: str,
        city: str = "",
        experience: str = "",
        salary: str = "",
        count: int = 30,
    ) -> list[dict]:
        """Return the legacy filtered card list to direct Python callers."""
        result = await self.scan_candidates(keyword, city, experience, salary, count)
        if result["stop_reason"] in ("search_error", "search_timeout") and not result["rows"]:
            return [{"error": result.get("error") or "等待新搜索结果超时"}]
        return [row["candidate"] for row in result["rows"] if not row["reasons"]][:count]

    async def scan_candidates(
        self,
        keyword: str,
        city: str = "",
        experience: str = "",
        salary: str = "",
        count: int = 30,
        scan_mode: str = "target",
        max_scrolls: int = 40,
    ) -> dict:
        """Return observed cards and why this keyword scan stopped."""
        evaluate = card_filter(city, experience, salary)
        if not keyword.strip():
            raise ValueError("搜索关键词不能为空")
        if not 1 <= count <= 300 or not 1 <= max_scrolls <= 200:
            raise ValueError("count 必须在 1 到 300 之间，max_scrolls 必须在 1 到 200 之间")
        if scan_mode not in ("target", "end"):
            raise ValueError("scan_mode 必须是 target 或 end")

        p = self.browser.page
        await self._cleanup_dialogs()
        frame = await self._get_search_frame(p)
        search_input = await frame.wait_for_selector("input.search-input")
        api_cards = {}
        response_seen = asyncio.Event()

        async def capture(response):
            url = response.url
            if not ("geeks.json" in url or "searchGeek" in url or ("zpgeek" in url and "search" in url)):
                return
            try:
                data = await response.json()
                candidates = data.get("zpData", {}).get("geekList", []) or data.get("data", {}).get("list", [])
                for card in candidates:
                    identifier = card.get("expectId") or card.get("encryptExpectId")
                    if identifier:
                        api_cards[str(identifier)] = card
                response_seen.set()
            except (AttributeError, TypeError, ValueError, RuntimeError, TimeoutError):
                log.debug("Search response data was unavailable")

        observed = {}
        current = []
        rounds = 0
        stall = 0
        verification = None
        error = None
        stop_reason = "search_timeout"
        p.on("response", capture)
        try:
            # A fresh response or a change to the result DOM anchors the scan to
            # this search. Do not attribute an unchanged old list to a new query.
            await search_input.click()
            await search_input.fill("")
            await search_input.type(keyword, delay=80)
            await _wait(0.5)
            before = await frame.evaluate(SEARCH_SNAPSHOT_JS)
            response_seen.clear()
            await search_input.press("Enter")
            await _wait(3)
            initial_deadline = time.monotonic() + 10
            while True:
                snapshot = await frame.evaluate(SEARCH_SNAPSHOT_JS)
                if (snapshot["cards"] or snapshot["empty"]) and (
                    response_seen.is_set() or snapshot != before
                ):
                    break
                verification = await self.browser.check_and_screenshot_verification()
                if verification or time.monotonic() >= initial_deadline:
                    return {"rows": [], "scroll_count": 0, "stop_reason": "verification" if verification else stop_reason,
                            "verification": verification}

            deadline = time.monotonic() + 90
            if snapshot["cards"] and await frame.evaluate(SCROLL_SEARCH_JS, True):
                await _wait(0.6)
                snapshot = await frame.evaluate(SEARCH_SNAPSHOT_JS)
            at_bottom = False
            while True:
                current = snapshot["cards"]
                prior = len(observed)
                for card in current:
                    identifier = (card.get("expectId") or "").strip()
                    card["expectId"] = identifier
                    # Missing IDs identify observations, not unique people.
                    key = identifier if identifier else (card["index"], card["fullText"])
                    observed[key] = card
                verification = await self.browser.check_and_screenshot_verification()
                if verification:
                    stop_reason = "verification"
                    break
                if snapshot["empty"] and not current:
                    stop_reason = "empty_results" if not observed else "list_unavailable"
                    break
                if snapshot["end"]:
                    stop_reason = "end_of_list"
                    break

                if scan_mode == "target":
                    eligible = sum(
                        bool(card["expectId"]) and not evaluate(_enrich_card(card, api_cards))
                        for card in observed.values()
                    )
                    if eligible >= count:
                        stop_reason = "target_reached"
                        break
                if time.monotonic() >= deadline:
                    stop_reason = "scan_timeout"
                    break
                if rounds and at_bottom and len(observed) == prior:
                    stall += 1
                else:
                    stall = 0
                if stall >= 3:
                    stop_reason = "no_new_cards"
                    break
                if rounds >= max_scrolls:
                    stop_reason = "scroll_limit"
                    break

                at_bottom = await frame.evaluate(SCROLL_SEARCH_JS)
                rounds += 1
                await _wait(2 if at_bottom else 0.6)
                snapshot = await frame.evaluate(SEARCH_SNAPSHOT_JS)
        except Exception as failure:
            stop_reason, error = "search_error", str(failure)
        finally:
            p.remove_listener("response", capture)

        current_indexes = {card["expectId"]: card["index"] for card in current if card.get("expectId")}
        rows = []
        for card in observed.values():
            candidate = _enrich_card(card, api_cards)
            candidate["index"] = current_indexes.get(candidate["expectId"])
            rows.append({"candidate": candidate, "reasons": evaluate(candidate)})
        return {"rows": rows, "scroll_count": rounds, "stop_reason": stop_reason,
                "verification": verification, "error": error}

    async def view_candidate_by_index(self, index: int) -> dict:
        if not isinstance(index, int) or index < 0:
            return {"error": "索引必须是非负整数"}
        return await self._view_card(index=index)

    async def view_candidate_by_expect_id(self, expect_id: str) -> dict:
        if not expect_id.strip():
            return {"error": "expect_id 不能为空", "error_code": "missing_id"}
        return await self._view_card(expect_id=expect_id.strip())

    async def _view_card(self, *, index=None, expect_id=None):
        p = self.browser.page
        # Close any existing dialog FIRST
        await self._cleanup_dialogs()

        # Ensure we have the search frame (with retry)
        if not self._search_frame:
            await self._get_search_frame(p)
        frame = self._search_frame
        if not frame:
            iframe_el = await p.query_selector("#searchContent iframe")
            if iframe_el:
                self._search_frame = await iframe_el.content_frame()
                frame = self._search_frame
        if not frame:
            return {"error": "搜索 iframe 不可用，请先执行搜索"}

        # Select and click in the same DOM operation. Virtualized rows can be
        # replaced between two separate protocol calls.
        clicked = await frame.evaluate("""({index, expectId}) => {
            const cards = Array.from(document.querySelectorAll('li.geek-info-card'));
            const links = cards.map(card => card.querySelector('a[data-contact]'));
            const matches = expectId === null ? [links[index]].filter(Boolean) :
                links.filter(link => link?.getAttribute('data-expect') === expectId);
            if (matches.length !== 1) return {count: matches.length};
            const card = matches[0];
            const ids = {expectId: card.getAttribute('data-expect') || '', jid: card.getAttribute('data-jid') || ''};
            card.click();
            return {index: links.indexOf(card), ids};
        }""", {"index": index, "expectId": expect_id})
        if "ids" not in clicked:
            if clicked["count"] > 1:
                return {"error": "当前列表存在多个相同 ID，无法确定目标简历", "error_code": "ambiguous_id"}
            if expect_id is not None:
                return {"error": "候选人卡片不在当前列表中", "error_code": "not_visible"}
            return {"error": f"索引 {index} 没有可查看的候选人卡片", "error_code": "not_visible"}
        index = clicked["index"]
        card_ids = clicked["ids"]

        await _wait(3)

        try:
            dialog = await p.wait_for_selector("div.boss-dialog__body, div.dialog-wrap.active", timeout=4000)
        except TimeoutError:
            return {"error": "未找到简历弹窗", "error_code": "dialog_missing"}

        # Read identity from this dialog, never from an unrelated element on
        # the page. A mismatched dialog is closed before capture/export.
        ids = await dialog._call("""function() {
            const selector = '[data-expectid], [data-geekid]';
            const el = this.querySelector('[data-expectid]') ||
                (this.matches(selector) ? this : this.querySelector(selector));
            if (!el) return {};
            return {
                geekId: el.getAttribute('data-geekid') || '',
                encryptUserId: el.getAttribute('data-encryptuserid') || '',
                expectId: el.getAttribute('data-expectid') || '',
                jid: el.getAttribute('data-jid') || ''
            };
        }""")
        expected = card_ids["expectId"]
        actual = ids.get("expectId", "")
        if expected and actual and expected != actual:
            await self._cleanup_dialogs()
            return {"error": "简历弹窗 ID 与目标卡片不一致", "error_code": "identity_mismatch"}
        identity = "verified" if expected and actual == expected else "card_only" if expected else "unavailable"
        if expect_id is not None and identity != "verified":
            await self._cleanup_dialogs()
            return {"error": "简历弹窗未提供可核对的候选人 ID", "error_code": "identity_unverified"}

        # Screenshot the dialog (captures the canvas-rendered resume)
        screenshot = await dialog.screenshot()

        # Check if resume is scrollable and capture full content
        resume_iframe_el = await p.query_selector("iframe[src*='c-resume']")
        all_screenshots = [screenshot]

        if resume_iframe_el:
            resume_frame = await resume_iframe_el.content_frame()
            if resume_frame:
                # Check canvas height vs container height
                scroll_info = await resume_frame.evaluate("""() => {
                    const canvas = document.querySelector('canvas');
                    const container = canvas?.parentElement;
                    if (!canvas || !container) return null;
                    return {
                        canvasH: canvas.height,
                        containerH: container.clientHeight,
                        styleH: parseInt(canvas.style.height) || 0
                    };
                }""")

                if scroll_info and scroll_info.get("canvasH", 0) > scroll_info.get("containerH", 0) * 2:
                    # Canvas is taller than viewport, need to scroll
                    container_h = scroll_info["containerH"]
                    canvas_h = scroll_info["styleH"] or scroll_info["canvasH"] // 2
                    scroll_steps = max(1, (canvas_h // container_h))

                    for step in range(1, min(scroll_steps + 1, 5)):
                        await resume_frame.evaluate(f"""() => {{
                            const canvas = document.querySelector('canvas');
                            if (canvas) {{
                                canvas.style.transform = 'translateY(-{step * container_h}px)';
                            }}
                        }}""")
                        await _wait(0.5)
                        shot = await dialog.screenshot()
                        all_screenshots.append(shot)

                    # Reset scroll
                    await resume_frame.evaluate("""() => {
                        const canvas = document.querySelector('canvas');
                        if (canvas) canvas.style.transform = 'translateY(0px)';
                    }""")

        ids = {**card_ids, **{key: value for key, value in ids.items() if value}}
        # Save screenshots to temp files
        paths = []
        for i, shot in enumerate(all_screenshots):
            path = os.path.join(SCREENSHOT_DIR, f"resume_{index}_{uuid.uuid4().hex}_p{i}.png")
            with open(path, "wb") as f:
                f.write(shot)
            os.chmod(path, 0o600)
            paths.append(path)

        # Extract share link by clicking the forward icon on canvas
        share_url = ""
        share_card_path = ""
        share_error = ""
        try:
            share_url, share_card_path = await self._extract_share_link(p, resume_iframe_el, index)
        except Exception as error:
            share_error = str(error)

        # Close the dialog via JS
        await self._cleanup_dialogs()
        await _wait(0.5)

        return {
            "screenshots": paths,
            "identity_check": identity,
            "ids": ids,
            "share_url": share_url,
            "share_card": share_card_path,
            "pages": len(paths),
            "share_error": share_error,
        }

    async def _extract_share_link(self, p, resume_iframe_el, index: int) -> tuple[str, str]:
        """Click the forward icon on canvas, extract QR code, decode share URL.

        Returns:
            (share_url, share_card_path) tuple
        """
        if not resume_iframe_el:
            return "", ""

        resume_frame = await resume_iframe_el.content_frame()
        if not resume_frame:
            return "", ""

        html_share = await p.query_selector(".btn-coop-forward")
        if html_share and await html_share.is_visible():
            await html_share.click()
        else:
            base_pos = await self._find_share_icon_position(resume_frame)
            if not base_pos:
                return "", ""
            bx, by = base_pos
            for fx, fy in [(bx, by), (bx+6, by), (bx-6, by), (bx, by+5), (bx+6, by+5)]:
                await resume_frame.evaluate(f"""() => {{
                    const canvas = document.querySelector('canvas');
                    if (!canvas) return;
                    const rect=canvas.getBoundingClientRect();
                    canvas.dispatchEvent(new MouseEvent('click', {{clientX:rect.left+{fx},clientY:rect.top+{fy},bubbles:true}}));
                }}""")
                await _wait(0.5)
                if await p.evaluate("Array.from(document.querySelectorAll('.c-pay-4-another')).some(e=>e.offsetWidth>0)"):
                    break
            else:
                return "", ""

        await _wait(0.5)

        # Click "转发至其他" to show QR code card
        await p.evaluate("""() => {
            const items = document.querySelectorAll('.c-pay-4-another .item, .nav-list .item');
            for (const item of items) {
                if ((item.innerText || '').includes('转发至其他')) {
                    item.click();
                    return true;
                }
            }
            return false;
        }""")
        await _wait(2)

        # Extract the share card image (base64 PNG with QR code)
        img_data = await p.evaluate("""() => {
            const img = document.querySelector('img.share-image');
            return img ? img.src : '';
        }""")

        share_url = ""
        share_card_path = ""

        if img_data.startswith("data:image/"):
            # Save share card image
            b64 = img_data.split(",", 1)[1]
            img_bytes = base64.b64decode(b64)
            share_card_path = os.path.join(SCREENSHOT_DIR, f"share_{index}_{uuid.uuid4().hex}.png")
            with open(share_card_path, "wb") as f:
                f.write(img_bytes)
            os.chmod(share_card_path, 0o600)

            # Decode QR code to get share URL
            if HAS_QR_DECODER:
                try:
                    share_url = decode_image(img_bytes)
                except Exception:
                    pass

        # Close the share panel (but keep resume dialog)
        await p.evaluate("""() => {
            const close=document.querySelector('.share-dialog-close');
            if(close && close.offsetWidth){close.click();return;}
            const panels = document.querySelectorAll('.c-pay-4-another');
            panels.forEach(panel => {
                const wrapper = panel.closest('.boss-popup__wrapper');
                if (wrapper) {
                    wrapper.remove();
                }
            });
        }""")
        await _wait(0.3)

        return share_url, share_card_path

    async def greet_by_index(self, index: int, message: str = "") -> dict:
        """Click the Nth candidate in search results and send a greeting.

        After greeting, the candidate appears in the "沟通" chat list permanently.

        Args:
            index: 0-based index of the candidate card
            message: Custom greeting message (optional)

        Returns:
            Result with status and candidate identifiers
        """
        p = self.browser.page
        if not isinstance(index, int) or index < 0:
            return {"status": "error", "message": "索引必须是非负整数"}

        # Close any existing dialog
        await self._cleanup_dialogs()

        # Get search frame
        if not self._search_frame:
            iframe_el = await p.query_selector("#searchContent iframe")
            if iframe_el:
                self._search_frame = await iframe_el.content_frame()
        frame = self._search_frame
        if not frame:
            return {"status": "error", "message": "搜索 iframe 不可用"}

        # Click the candidate card
        clicked = await frame.evaluate(f"""() => {{
            const cards = document.querySelectorAll("li.geek-info-card a[data-contact]");
            if ({index} >= cards.length) return false;
            cards[{index}].click();
            return true;
        }}""")

        if not clicked:
            return {"status": "error", "message": f"索引 {index} 超出范围"}

        await _wait(3)

        # Extract candidate identifiers from dialog
        ids = await p.evaluate("""() => {
            const el = document.querySelector('[data-geekid]');
            if (!el) return {};
            return {
                geekId: el.getAttribute('data-geekid') || '',
                expectId: el.getAttribute('data-expectid') || '',
                jid: el.getAttribute('data-jid') || '',
            };
        }""")

        # Click "联系Ta" button
        try:
            greet_btn = await p.query_selector("button.btn-getcontact, .btn-getcontact")
            if not greet_btn:
                await self._cleanup_dialogs()
                return {"status": "error", "message": "未找到联系按钮", "ids": ids}

            await greet_btn.click()
            await _wait(3)

            # Check if a chat window / message input appeared
            # If custom message, try to type and send
            if message:
                chat_input = await p.query_selector("textarea, .chat-input, [contenteditable='true']")
                if chat_input:
                    await chat_input.fill("")
                    await chat_input.type(message, delay=50)
                    await _wait(0.5)
                    send_btn = await p.query_selector("button:has-text('发送'), .btn-send")
                    if send_btn:
                        await send_btn.click()
                        await _wait(1)
                    else:
                        await self._cleanup_dialogs()
                        return {"status": "partial", "greeting_sent": True, "message": "已发起沟通，但未找到自定义消息的发送按钮", "ids": ids}
                else:
                    await self._cleanup_dialogs()
                    return {"status": "partial", "greeting_sent": True, "message": "已发起沟通，但未找到自定义消息输入框", "ids": ids}

            result = {"status": "success", "message": "已发送招呼，候选人已进入沟通列表", "ids": ids}

        except Exception as e:
            result = {"status": "error", "message": str(e), "ids": ids}

        # Close dialog
        await self._cleanup_dialogs()

        return result

    async def view_candidate(self, profile_url: str) -> dict:
        """View detailed candidate resume by URL (legacy, may not work in SPA)."""
        self._validate_profile_url(profile_url)
        p = self.browser.page
        await p.goto(profile_url, wait_until="domcontentloaded")
        await self.browser.random_delay()

        resume = {"profile_url": profile_url}
        try:
            body = await p.query_selector("body")
            if body:
                resume["full_text"] = (await body.inner_text())[:5000]
        except Exception as e:
            resume["error"] = str(e)
        return resume

    async def send_greeting(self, profile_url: str, message: str = "") -> dict:
        """Send a greeting message to a candidate."""
        self._validate_profile_url(profile_url)
        p = self.browser.page
        await p.goto(profile_url, wait_until="domcontentloaded")
        await self.browser.random_delay()

        try:
            greet_btn = await p.query_selector(
                "button:has-text('打招呼'), button:has-text('沟通'), .btn-greet, .btn-chat"
            )
            if not greet_btn:
                return {"status": "error", "message": "未找到打招呼按钮"}

            await greet_btn.click()
            await self.browser.random_delay()

            if message:
                input_el = await p.query_selector("textarea, .chat-input, [contenteditable]")
                if input_el:
                    await input_el.fill("")
                    await input_el.type(message, delay=50)
                    await self.browser.random_delay()
                    send_btn = await p.query_selector("button:has-text('发送'), .btn-send")
                    if send_btn:
                        await send_btn.click()
                    else:
                        return {"status": "partial", "greeting_sent": True, "message": "已点击打招呼，但未找到自定义消息发送按钮"}
                else:
                    return {"status": "partial", "greeting_sent": True, "message": "已点击打招呼，但未找到自定义消息输入框"}

            return {"status": "success", "message": "已向候选人发送消息"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @staticmethod
    def _validate_profile_url(url):
        parsed = urlsplit(url)
        base = urlsplit(BOSS_BASE_URL)
        hostname = parsed.hostname or ""
        allowed = hostname == base.hostname or hostname.endswith(".zhipin.com") or hostname == "zpurl.cn"
        if parsed.scheme not in ("http", "https") or not allowed:
            raise ValueError("profile_url 必须是 BOSS 直聘或 zpurl.cn 的有效链接")
