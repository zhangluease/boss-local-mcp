"""Small Chrome CDP client for the DOM operations used by the recruiter tools.

Runtime.enable and console subscriptions are intentionally unnecessary: commands
evaluate only the requested expression and leave page console objects untouched.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

import websockets


class CDPError(RuntimeError):
    pass


class CDPConnection:
    def __init__(self, socket, browser_version=""):
        self.socket = socket
        self.browser_version = browser_version
        self._next_id = 0
        self._pending = {}
        self._listeners = {}
        self._tasks = set()
        self._closed = False
        self._reader = asyncio.create_task(self._receive())

    @classmethod
    async def connect(cls, endpoint: str):
        def read_version():
            # Local Chrome must not be routed through a system HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(endpoint.rstrip("/") + "/json/version", timeout=5) as response:
                return json.load(response)

        version = await asyncio.to_thread(read_version)
        socket = await websockets.connect(
            version["webSocketDebuggerUrl"], proxy=None, max_size=32 * 1024 * 1024,
            open_timeout=5,
        )
        return cls(socket, browser_version=version.get("Browser", ""))

    async def _receive(self):
        try:
            async for message in self.socket:
                item = json.loads(message)
                if "id" in item:
                    future = self._pending.pop(item["id"], None)
                    if future and not future.done():
                        if "error" in item:
                            future.set_exception(CDPError(item["error"].get("message", "CDP command failed")))
                        else:
                            future.set_result(item.get("result", {}))
                    continue
                key = (item.get("sessionId"), item.get("method"))
                for callback in list(self._listeners.get(key, ())):
                    try:
                        result = callback(item.get("params", {}))
                        if inspect.isawaitable(result):
                            task = asyncio.create_task(result)
                            self._tasks.add(task)
                            task.add_done_callback(self._task_done)
                    except Exception:
                        # An optional event listener must not stop the protocol reader.
                        continue
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPError("Chrome connection closed"))
            self._pending.clear()

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve listener failures without leaking task warnings.

    async def command(self, method, params=None, session=None, timeout=30):
        if self._closed:
            raise CDPError("Chrome connection closed")
        self._next_id += 1
        identifier = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        payload = {"id": identifier, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        try:
            await self.socket.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(identifier, None)

    def on(self, session, method, callback):
        self._listeners.setdefault((session, method), []).append(callback)

    def off(self, session, method, callback):
        listeners = self._listeners.get((session, method), [])
        if callback in listeners:
            listeners.remove(callback)

    def is_connected(self):
        return not self._closed

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        await self.socket.close()
        await self._reader

    async def pages(self):
        targets = await self.command("Target.getTargets")
        return [target for target in targets["targetInfos"] if target["type"] == "page"]

    async def attach(self, target):
        result = await self.command("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        page = CDPPage(self, result["sessionId"], target["targetId"], target.get("url", ""))
        await self.command("Page.enable", session=page.session)
        return page


_SELECT = r"""function(root, selector) {
    const groups = selector.split(/,(?![^\[]*\])/);
    const found = [];
    for (const group of groups) {
        const match = group.match(/:has-text\((['"])(.*?)\1\)/);
        const css = match ? group.replace(match[0], '') : group;
        for (const element of root.querySelectorAll(css.trim())) {
            if ((!match || (element.textContent || '').includes(match[2])) && !found.includes(element)) found.push(element);
        }
    }
    return found;
}"""

_VISIBLE = """function() {
    const rect = this.getBoundingClientRect();
    const style = getComputedStyle(this);
    return !!(rect.width && rect.height && style.display !== 'none' && style.visibility !== 'hidden');
}"""


def _unpack(result):
    if result.get("exceptionDetails"):
        error = result["exceptionDetails"]
        raise CDPError(error.get("exception", {}).get("description", error.get("text", "JavaScript evaluation failed")))
    remote = result.get("result", {})
    if remote.get("subtype") == "null" or remote.get("type") == "undefined":
        return None
    return remote.get("value")


class CDPFrame:
    def __init__(self, page, frame_id=None, owner=None):
        self.page = page
        self.frame_id = frame_id
        self._context_id = None
        self._session = page.session
        self.owner = owner

    async def _context(self):
        if not self.frame_id:
            return None
        if self._context_id:
            return self._context_id
        try:
            result = await self.page.connection.command("Page.createIsolatedWorld", {
                "frameId": self.frame_id, "worldName": "boss-local-mcp",
            }, session=self._session)
        except CDPError:
            targets = (await self.page.connection.command("Target.getTargets"))["targetInfos"]
            target = next((target for target in targets if target["targetId"] == self.frame_id), None)
            if not target:
                raise
            attached = await self.page.connection.command("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
            self._session = attached["sessionId"]
            result = await self.page.connection.command("Page.createIsolatedWorld", {
                "frameId": self.frame_id, "worldName": "boss-local-mcp",
            }, session=self._session)
        self._context_id = result["executionContextId"]
        return self._context_id

    async def _evaluate(self, expression, return_by_value=True):
        for attempt in range(2):
            params = {"expression": expression, "returnByValue": return_by_value, "awaitPromise": True}
            context_id = await self._context()
            if context_id:
                params["contextId"] = context_id
            try:
                result = await self.page.connection.command("Runtime.evaluate", params, self._session)
                return _unpack(result) if return_by_value else result.get("result", {})
            except CDPError as error:
                if attempt or "context" not in str(error).lower():
                    raise
                self._context_id = None

    async def evaluate(self, expression: str, arg: Any = None):
        source = expression.strip()
        if re.match(r"^(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)", source):
            source = f"({source})({json.dumps(arg, ensure_ascii=False)})"
        return await self._evaluate(source)

    async def _element(self, expression):
        remote = await self._evaluate(expression, False)
        if remote.get("objectId") and remote.get("subtype") != "null":
            return CDPElement(self, remote["objectId"])
        return None

    async def query_selector(self, selector):
        return await self._element(f"({_SELECT})(document, {json.dumps(selector)})[0]")

    async def query_selector_all(self, selector):
        expression = f"({_SELECT})(document, {json.dumps(selector)})"
        count = await self._evaluate(expression + ".length")
        return [await self._element(f"{expression}[{index}]") for index in range(count)]

    async def wait_for_selector(self, selector, timeout=10000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            element = await self.query_selector(selector)
            if element and await element.is_visible():
                return element
            await asyncio.sleep(0.2)
        raise TimeoutError(f"Element did not become visible: {selector}")

    def locator(self, selector):
        return CDPLocator(self, selector)

    async def viewport_offset(self):
        if not self.owner:
            return {"x": 0, "y": 0}
        rectangle = await self.owner.bounding_box()
        border = await self.owner._call("function(){return {x:this.clientLeft,y:this.clientTop};}")
        return {"x": rectangle["x"] + border["x"], "y": rectangle["y"] + border["y"]}


class CDPPage(CDPFrame):
    def __init__(self, connection, session, target_id, url):
        self.connection = connection
        self.session = session
        self.target_id = target_id
        self._url = url
        self._closed = False
        self._network_enabled = False
        self._response_handlers = {}
        self._finished_requests = {}
        super().__init__(self)
        connection.on(session, "Page.frameNavigated", self._navigated)
        connection.on(None, "Target.detachedFromTarget", self._detached)
        connection.on(session, "Inspector.detached", lambda event: setattr(self, "_closed", True))
        connection.on(session, "Network.loadingFinished", self._loading_finished)

    @property
    def url(self):
        return self._url

    def is_closed(self):
        return self._closed or not self.connection.is_connected()

    def _navigated(self, event):
        if not event["frame"].get("parentId"):
            self._url = event["frame"]["url"]

    def _detached(self, event):
        if event.get("sessionId") == self.session:
            self._closed = True

    def _loading_finished(self, event):
        waiter = self._finished_requests.get(event["requestId"])
        if waiter:
            waiter.set()

    async def goto(self, url, wait_until="domcontentloaded", timeout=45000):
        result = await self.connection.command("Page.navigate", {"url": url}, self.session)
        if result.get("errorText"):
            raise CDPError(result["errorText"])
        self._url = url
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            try:
                state = await self.evaluate("({ready:document.readyState,url:location.href})")
                if state and state["url"] != "about:blank" and state["ready"] in ("interactive", "complete"):
                    self._url = state["url"]
                    return
            except CDPError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError("Page navigation timed out")

    async def title(self):
        return await self.evaluate("document.title")

    async def bring_to_front(self):
        await self.connection.command("Page.bringToFront", session=self.session)

    async def screenshot(self, path=None, full_page=False, clip=None):
        params = {"format": "png", "captureBeyondViewport": bool(full_page or clip)}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        elif full_page:
            metrics = await self.connection.command("Page.getLayoutMetrics", session=self.session)
            params["clip"] = {**metrics["cssContentSize"], "scale": 1}
        result = await self.connection.command("Page.captureScreenshot", params, self.session)
        data = base64.b64decode(result["data"])
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        return data

    async def wait_for_timeout(self, milliseconds):
        await asyncio.sleep(milliseconds / 1000)

    def on(self, event, callback):
        if event != "response":
            raise ValueError(f"Unsupported page event: {event}")

        async def handler(params):
            response = CDPResponse(self, params["requestId"], params["response"]["url"])
            await callback(response)

        self._response_handlers[callback] = handler
        self.connection.on(self.session, "Network.responseReceived", handler)
        if not self._network_enabled:
            task = asyncio.create_task(self.connection.command("Network.enable", session=self.session))
            self.connection._tasks.add(task)
            task.add_done_callback(self.connection._task_done)
            self._network_enabled = True

    def remove_listener(self, event, callback):
        handler = self._response_handlers.pop(callback, None)
        if handler:
            self.connection.off(self.session, "Network.responseReceived", handler)


class CDPResponse:
    def __init__(self, page, request_id, url):
        self.page, self.request_id, self.url = page, request_id, url

    async def json(self):
        waiter = self.page._finished_requests.setdefault(self.request_id, asyncio.Event())
        try:
            try:
                result = await self.page.connection.command("Network.getResponseBody", {"requestId": self.request_id}, self.page.session)
            except CDPError:
                await asyncio.wait_for(waiter.wait(), 10)
                result = await self.page.connection.command("Network.getResponseBody", {"requestId": self.request_id}, self.page.session)
            body = result["body"]
            if result.get("base64Encoded"):
                body = base64.b64decode(body).decode()
            return json.loads(body)
        finally:
            self.page._finished_requests.pop(self.request_id, None)


class CDPElement:
    def __init__(self, frame, object_id):
        self.frame, self.object_id = frame, object_id

    async def _call(self, function, arguments=None):
        result = await self.frame.page.connection.command("Runtime.callFunctionOn", {
            "objectId": self.object_id, "functionDeclaration": function,
            "arguments": [{"value": value} for value in (arguments or [])],
            "returnByValue": True, "awaitPromise": True,
        }, self.frame._session)
        return _unpack(result)

    async def is_visible(self):
        return await self._call(_VISIBLE)

    async def click(self):
        await self._call("function(){this.scrollIntoView({block:'center'});}")
        rectangle = await self.bounding_box()
        if rectangle["width"] <= 0 or rectangle["height"] <= 0:
            raise CDPError("Cannot click an invisible element")
        unobstructed = await self._call("""function(){
            const r=this.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            return hit===this||this.contains(hit);
        }""")
        if not unobstructed:
            raise CDPError("The requested element is covered by another element")
        point = {"x": rectangle["x"] + rectangle["width"] / 2, "y": rectangle["y"] + rectangle["height"] / 2}
        owner = self.frame.owner
        while owner:
            offset = await owner.frame.viewport_offset()
            visible_owner = await owner._call("""function(x,y){const hit=document.elementFromPoint(x,y);return hit===this||this.contains(hit);}""", [point["x"] - offset["x"], point["y"] - offset["y"]])
            if not visible_owner:
                raise CDPError("The iframe is covered by another page element")
            owner = owner.frame.owner
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, **point}
            if kind != "mouseMoved":
                params.update(button="left", clickCount=1)
            await self.frame.page.connection.command("Input.dispatchMouseEvent", params, self.frame.page.session)

    async def bounding_box(self):
        rectangle = await self._call("function(){const r=this.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};}")
        if self.frame.owner:
            owner = await self.frame.owner.bounding_box()
            border = await self.frame.owner._call("function(){return {x:this.clientLeft,y:this.clientTop};}")
            rectangle["x"] += owner["x"] + border["x"]
            rectangle["y"] += owner["y"] + border["y"]
        return rectangle

    async def fill(self, text):
        await self._call("""function(text) {
            this.focus();
            if (this.isContentEditable) this.textContent = text;
            else {
                const proto = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(this, text); else this.value = text;
            }
            this.dispatchEvent(new Event('input', {bubbles:true}));
            this.dispatchEvent(new Event('change', {bubbles:true}));
        }""", [text])

    async def type(self, text, delay=0):
        await self._call("function(){this.focus();}")
        chunks = list(text) if delay else [text]
        for chunk in chunks:
            await self.frame.page.connection.command("Input.insertText", {"text": chunk}, self.frame.page.session)
            if delay:
                await asyncio.sleep(delay / 1000)

    async def press(self, key):
        code = {"Enter": ("Enter", 13), "Escape": ("Escape", 27), "Tab": ("Tab", 9)}.get(key)
        if not code:
            raise ValueError(f"Unsupported key: {key}")
        await self._call("function(){this.focus();}")
        for kind in ("keyDown", "keyUp"):
            params = {
                "type": kind, "key": key, "code": code[0],
                "windowsVirtualKeyCode": code[1], "nativeVirtualKeyCode": code[1],
            }
            if key == "Enter" and kind == "keyDown":
                params.update(text="\r", unmodifiedText="\r")
            await self.frame.page.connection.command("Input.dispatchKeyEvent", params, self.frame.page.session)

    async def inner_text(self):
        return await self._call("function(){return this.innerText || '';}")

    async def content_frame(self):
        result = await self.frame.page.connection.command("DOM.describeNode", {"objectId": self.object_id, "depth": 1}, self.frame._session)
        node = result["node"]
        frame_id = node.get("frameId") or node.get("contentDocument", {}).get("frameId")
        return CDPFrame(self.frame.page, frame_id, owner=self) if frame_id else None

    async def screenshot(self, path=None):
        rectangle = await self.bounding_box()
        scroll = await self.frame.page.evaluate("({x:scrollX,y:scrollY})")
        rectangle["x"] += scroll["x"]
        rectangle["y"] += scroll["y"]
        if rectangle["width"] <= 0 or rectangle["height"] <= 0:
            raise CDPError("Cannot screenshot an invisible element")
        return await self.frame.page.screenshot(path=path, clip=rectangle)


class CDPLocator:
    def __init__(self, frame, selector):
        self.frame, self.selector = frame, selector

    async def all(self):
        return await self.frame.query_selector_all(self.selector)

    async def count(self):
        return len(await self.all())
