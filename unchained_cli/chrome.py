"""Direct Chrome DevTools Protocol (CDP) client.

Talks to local Chrome over WebSocket — no relay, no auth.
Chrome must be launched with --remote-debugging-port=<port>.
"""
from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from typing import Any

_CONNECT_TIMEOUT = 5.0   # seconds to open HTTP connection to Chrome
_CMD_TIMEOUT     = 30.0  # seconds to wait for a CDP command response


class CDPError(RuntimeError):
    pass


class ChromeClient:
    """Minimal CDP client that talks directly to local Chrome."""

    def __init__(self, port: int = 9222):
        self.port = port
        self._base = f"http://localhost:{port}"

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def list_tabs(self) -> list[dict]:
        """Return all open page tabs."""
        try:
            with urllib.request.urlopen(
                f"{self._base}/json", timeout=_CONNECT_TIMEOUT
            ) as r:
                return [t for t in json.loads(r.read()) if t.get("type") == "page"]
        except urllib.error.URLError as exc:
            raise CDPError(
                f"Cannot connect to Chrome at localhost:{self.port}. "
                f"Start Chrome with: --remote-debugging-port={self.port}"
            ) from exc

    def resolve_tab(self, tab_id: str = "auto") -> str:
        """Return a concrete tab ID, resolving 'auto' to the first page tab."""
        tabs = self.list_tabs()
        if not tabs:
            raise CDPError("No page tabs open in Chrome.")
        if tab_id == "auto":
            return tabs[0]["id"]
        for t in tabs:
            if t["id"] == tab_id:
                return t["id"]
        raise CDPError(f"Tab not found: {tab_id!r}")

    def _ws_url_for(self, tab_id: str) -> str:
        try:
            with urllib.request.urlopen(
                f"{self._base}/json/{tab_id}", timeout=_CONNECT_TIMEOUT
            ) as r:
                data = json.loads(r.read())
                ws = data.get("webSocketDebuggerUrl")
                if ws:
                    return ws
        except Exception:
            pass
        # Fallback: scan the full list
        for t in self.list_tabs():
            if t["id"] == tab_id:
                ws = t.get("webSocketDebuggerUrl")
                if ws:
                    return ws
        raise CDPError(f"No WebSocket URL for tab {tab_id!r}")

    # ------------------------------------------------------------------
    # Low-level command dispatch
    # ------------------------------------------------------------------

    def send(
        self,
        tab_id: str,
        method: str,
        params: dict | None = None,
        *,
        wait_for_event: str | None = None,
        timeout: float = _CMD_TIMEOUT,
    ) -> dict:
        """Send a CDP command and return the result dict."""
        ws_url = self._ws_url_for(tab_id)
        return asyncio.run(
            self._async_send(ws_url, method, params or {}, wait_for_event, timeout)
        )

    async def _async_send(
        self,
        ws_url: str,
        method: str,
        params: dict,
        wait_for_event: str | None,
        timeout: float,
    ) -> dict:
        try:
            import websockets
        except ImportError:
            raise CDPError(
                "Missing dependency 'websockets'. Run: pip install websockets"
            )

        async def _run() -> dict:
            async with websockets.connect(ws_url, ping_timeout=None) as ws:
                if wait_for_event:
                    domain = wait_for_event.split(".")[0]
                    await ws.send(
                        json.dumps({"id": 0, "method": f"{domain}.enable", "params": {}})
                    )

                cmd_id = 1
                await ws.send(
                    json.dumps({"id": cmd_id, "method": method, "params": params})
                )

                result: dict | None = None
                event_done = not bool(wait_for_event)

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("id") == cmd_id:
                        if "error" in msg:
                            raise CDPError(msg["error"]["message"])
                        result = msg.get("result", {})
                    if wait_for_event and msg.get("method") == wait_for_event:
                        event_done = True
                    if result is not None and event_done:
                        return result

                return result or {}

        return await asyncio.wait_for(_run(), timeout=timeout)

    # ------------------------------------------------------------------
    # Higher-level helpers
    # ------------------------------------------------------------------

    def navigate(self, tab_id: str, url: str) -> dict:
        return self.send(
            tab_id, "Page.navigate", {"url": url},
            wait_for_event="Page.loadEventFired",
        )

    def click(self, tab_id: str, x: int, y: int) -> None:
        for event_type in ("mousePressed", "mouseReleased"):
            self.send(tab_id, "Input.dispatchMouseEvent", {
                "type": event_type,
                "x": x, "y": y,
                "button": "left",
                "clickCount": 1,
            })

    def click_selector(self, tab_id: str, selector: str) -> dict:
        """Click an element identified by CSS selector."""
        expr = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return {{error: "Element not found: {selector}"}};
            var r = el.getBoundingClientRect();
            return {{x: r.left + r.width/2, y: r.top + r.height/2}};
        }})()
        """
        result = self.js_eval(tab_id, expr)
        if isinstance(result, dict) and "error" in result:
            raise CDPError(result["error"])
        if not isinstance(result, dict) or "x" not in result:
            raise CDPError(f"Could not locate element: {selector!r}")
        self.click(tab_id, int(result["x"]), int(result["y"]))
        return result

    def type_text(self, tab_id: str, text: str) -> None:
        self.send(tab_id, "Input.insertText", {"text": text})

    def scroll(self, tab_id: str, direction: str, amount: int) -> None:
        delta_map = {
            "down":  (0,  amount),
            "up":    (0, -amount),
            "right": (amount,  0),
            "left":  (-amount, 0),
        }
        dx, dy = delta_map[direction]
        self.send(tab_id, "Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": 400, "y": 300,
            "deltaX": dx, "deltaY": dy,
        })

    def screenshot(self, tab_id: str) -> bytes:
        result = self.send(tab_id, "Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    def js_eval(self, tab_id: str, expression: str) -> Any:
        result = self.send(tab_id, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        obj = result.get("result", {})
        if obj.get("subtype") == "error":
            raise CDPError(obj.get("description", "JS error"))
        val = obj.get("value")
        # If JSON string returned from expression, parse it
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError):
                pass
        return val

    def js_eval_frame(self, tab_id: str, frame_id: str, expression: str) -> Any:
        result = self.send(tab_id, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "contextId": int(frame_id) if frame_id.isdigit() else None,
        })
        obj = result.get("result", {})
        if obj.get("subtype") == "error":
            raise CDPError(obj.get("description", "JS error"))
        return obj.get("value")

    def key_press(self, tab_id: str, key: str, modifiers: int = 0) -> None:
        for event_type in ("keyDown", "keyUp"):
            self.send(tab_id, "Input.dispatchKeyEvent", {
                "type": event_type,
                "key": key,
                "modifiers": modifiers,
            })

    def wait_ready(self, tab_id: str, strategy: str = "both", timeout: float = 30.0) -> str:
        if strategy in ("dom", "both"):
            # Poll document.readyState
            import time
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = self.send(tab_id, "Runtime.evaluate", {
                    "expression": "document.readyState",
                    "returnByValue": True,
                })
                if state.get("result", {}).get("value") == "complete":
                    break
                time.sleep(0.2)
        if strategy in ("network", "both"):
            self.send(
                tab_id, "Page.navigate",
                {"url": "javascript:void(0)"},  # no-op nav just to flush events
                wait_for_event="Page.loadEventFired",
                timeout=timeout,
            )
        return "ready"

    def get_cookies(self, tab_id: str, urls: list[str] | None = None) -> list[dict]:
        if urls:
            result = self.send(tab_id, "Network.getCookies", {"urls": urls})
        else:
            result = self.send(tab_id, "Network.getCookies", {})
        return result.get("cookies", [])

    def set_cookies(self, tab_id: str, cookies: list[dict]) -> None:
        self.send(tab_id, "Network.setCookies", {"cookies": cookies})

    def list_frames(self, tab_id: str) -> list[dict]:
        result = self.send(tab_id, "Page.getFrameTree", {})
        frames = []
        def _walk(node: dict, idx: list[int]):
            f = node.get("frame", {})
            frames.append({
                "index": idx[0],
                "frameId": f.get("id", ""),
                "url": f.get("url", ""),
                "name": f.get("name", ""),
            })
            idx[0] += 1
            for child in node.get("childFrames", []):
                _walk(child, idx)
        _walk(result.get("frameTree", {}), [0])
        return frames
