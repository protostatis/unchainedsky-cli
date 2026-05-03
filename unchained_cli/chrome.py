"""Direct Chrome DevTools Protocol (CDP) client.

Talks to local Chrome over WebSocket — no relay, no auth.
Chrome must be launched with --remote-debugging-port=<port>.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_CONNECT_TIMEOUT = 5.0   # seconds to open HTTP connection to Chrome
_CMD_TIMEOUT     = 30.0  # seconds to wait for a CDP command response
_DOM_POLL_INTERVAL = 0.2
_NETWORK_IDLE_WINDOW = 0.5


class CDPError(RuntimeError):
    pass


_ALIASES_DIR = Path.home() / ".unchained"


class ChromeClient:
    """Minimal CDP client that talks directly to local Chrome."""

    def __init__(self, port: int = 9222):
        self.port = port
        self._base = f"http://localhost:{port}"
        self._aliases: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def list_tabs(self) -> list[dict]:
        """Return all open page tabs."""
        try:
            with urllib.request.urlopen(
                f"{self._base}/json", timeout=_CONNECT_TIMEOUT
            ) as r:
                return [
                    t for t in json.loads(r.read())
                    if t.get("type") == "page"
                    and not (t.get("url") or "").startswith(("chrome://", "devtools://"))
                ]
        except urllib.error.URLError as exc:
            raise CDPError(
                f"Cannot connect to Chrome at localhost:{self.port}. "
                f"Start Chrome with: --remote-debugging-port={self.port}"
            ) from exc

    def resolve_tab(self, tab_id: str = "auto") -> str:
        """Return a concrete tab ID, resolving 'auto' or aliases."""
        tabs = self.list_tabs()
        if not tabs:
            raise CDPError("No page tabs open in Chrome.")
        if tab_id == "auto":
            return tabs[0]["id"]
        for t in tabs:
            if t["id"] == tab_id:
                return t["id"]
        # Try alias resolution
        aliases = self.load_aliases()
        if tab_id in aliases:
            resolved = aliases[tab_id]
            for t in tabs:
                if t["id"] == resolved:
                    return resolved
            raise CDPError(f"Alias {tab_id!r} points to {resolved!r} which no longer exists.")
        raise CDPError(f"Tab not found: {tab_id!r}")

    # ------------------------------------------------------------------
    # Tab aliases
    # ------------------------------------------------------------------

    def _aliases_path(self) -> Path:
        return _ALIASES_DIR / f"aliases_{self.port}.json"

    def load_aliases(self) -> dict[str, str]:
        if self._aliases is not None:
            return self._aliases
        path = self._aliases_path()
        if path.is_file():
            try:
                self._aliases = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._aliases = {}
        else:
            self._aliases = {}
        return self._aliases

    def save_aliases(self) -> None:
        path = self._aliases_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.load_aliases(), indent=2))

    def set_alias(self, name: str, tab_id: str) -> None:
        self.load_aliases()[name] = tab_id
        self.save_aliases()

    def delete_alias(self, name: str) -> None:
        aliases = self.load_aliases()
        if name not in aliases:
            raise CDPError(f"Alias not found: {name!r}")
        del aliases[name]
        self.save_aliases()

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
        try:
            return asyncio.run(
                self._async_send(ws_url, method, params or {}, wait_for_event, timeout)
            )
        except asyncio.TimeoutError as exc:
            detail = f"Timed out after {timeout:.1f}s while waiting for {method}"
            if wait_for_event:
                detail += f" / {wait_for_event}"
            raise CDPError(detail) from exc
        except OSError as exc:
            raise CDPError(f"Could not connect to Chrome tab websocket: {exc}") from exc

    @staticmethod
    def _require_websockets():
        try:
            import websockets
        except ImportError as exc:
            raise CDPError(
                "Missing dependency 'websockets'. Run: pip install websockets"
            ) from exc
        return websockets

    @staticmethod
    def _runtime_value(result: dict) -> Any:
        obj = result.get("result", {})
        if obj.get("subtype") == "error":
            raise CDPError(obj.get("description", "JS error"))
        return obj.get("value")

    async def _async_send(
        self,
        ws_url: str,
        method: str,
        params: dict,
        wait_for_event: str | None,
        timeout: float,
    ) -> dict:
        websockets = self._require_websockets()

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
        # Chrome 147+ AIM popup hijacks navigation on background tabs; bring
        # the tab to front first to ensure the navigate lands on the right target.
        try:
            self.send(tab_id, "Page.bringToFront")
        except CDPError:
            pass
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
        val = self._runtime_value(result)
        # If JSON string returned from expression, parse it
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError):
                pass
        return val

    def _resolve_frame_id(self, tab_id: str, frame_id: str) -> str:
        if frame_id.isdigit():
            index = int(frame_id)
            for frame in self.list_frames(tab_id):
                if frame["index"] == index:
                    return frame["frameId"]
            raise CDPError(f"Frame index not found: {frame_id}")

        for frame in self.list_frames(tab_id):
            if frame["frameId"] == frame_id:
                return frame_id
        raise CDPError(f"Frame not found: {frame_id}")

    def js_eval_frame(self, tab_id: str, frame_id: str, expression: str) -> Any:
        resolved_frame_id = self._resolve_frame_id(tab_id, frame_id)
        world = self.send(tab_id, "Page.createIsolatedWorld", {
            "frameId": resolved_frame_id,
            "worldName": "unchained_cli",
        })
        context_id = world.get("executionContextId")
        if context_id is None:
            raise CDPError(f"Could not create execution context for frame: {frame_id}")

        result = self.send(tab_id, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "contextId": context_id,
        })
        return self._runtime_value(result)

    def key_press(self, tab_id: str, key: str, modifiers: int = 0) -> None:
        for event_type in ("keyDown", "keyUp"):
            self.send(tab_id, "Input.dispatchKeyEvent", {
                "type": event_type,
                "key": key,
                "modifiers": modifiers,
            })

    def wait_ready(self, tab_id: str, strategy: str = "both", timeout: float = 30.0) -> str:
        ws_url = self._ws_url_for(tab_id)
        try:
            return asyncio.run(self._async_wait_ready(ws_url, strategy, timeout))
        except asyncio.TimeoutError as exc:
            raise CDPError(
                f"Timed out after {timeout:.1f}s while waiting for page readiness "
                f"(strategy={strategy})"
            ) from exc

    async def _async_wait_ready(
        self,
        ws_url: str,
        strategy: str,
        timeout: float,
    ) -> str:
        websockets = self._require_websockets()
        wants_dom = strategy in ("dom", "both")
        wants_network = strategy in ("network", "both")

        async with websockets.connect(ws_url, ping_timeout=None) as ws:
            await ws.send(json.dumps({"id": 0, "method": "Runtime.enable", "params": {}}))
            if wants_network:
                await ws.send(json.dumps({"id": 0, "method": "Network.enable", "params": {}}))

            next_cmd_id = 1
            pending_dom_check_id: int | None = None
            next_dom_poll_at = 0.0
            inflight_requests: set[str] = set()
            last_network_activity = time.monotonic()
            deadline = time.monotonic() + timeout
            dom_ready = not wants_dom

            async def send_dom_check() -> None:
                nonlocal next_cmd_id, pending_dom_check_id, next_dom_poll_at
                if not wants_dom or dom_ready or pending_dom_check_id is not None:
                    return
                pending_dom_check_id = next_cmd_id
                next_cmd_id += 1
                next_dom_poll_at = time.monotonic() + _DOM_POLL_INTERVAL
                await ws.send(json.dumps({
                    "id": pending_dom_check_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "document.readyState",
                        "returnByValue": True,
                    },
                }))

            await send_dom_check()

            while True:
                now = time.monotonic()
                network_ready = (
                    not wants_network
                    or (
                        not inflight_requests
                        and now - last_network_activity >= _NETWORK_IDLE_WINDOW
                    )
                )
                if dom_ready and network_ready:
                    return "ready"
                if now >= deadline:
                    raise asyncio.TimeoutError
                if wants_dom and not dom_ready and pending_dom_check_id is None and now >= next_dom_poll_at:
                    await send_dom_check()

                recv_timeout = min(_DOM_POLL_INTERVAL, max(0.0, deadline - now))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    continue

                msg = json.loads(raw)

                if msg.get("id") == pending_dom_check_id:
                    pending_dom_check_id = None
                    dom_ready = self._runtime_value(msg.get("result", {})) == "complete"
                    continue

                if not wants_network:
                    continue

                method = msg.get("method")
                params = msg.get("params", {})
                request_id = params.get("requestId")

                if method == "Network.requestWillBeSent" and request_id:
                    inflight_requests.add(request_id)
                    last_network_activity = time.monotonic()
                elif method in ("Network.loadingFinished", "Network.loadingFailed") and request_id:
                    inflight_requests.discard(request_id)
                    last_network_activity = time.monotonic()

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

    # ------------------------------------------------------------------
    # Tab lifecycle
    # ------------------------------------------------------------------

    def create_tab(self, url: str = "about:blank") -> dict:
        """Open a new tab and return its info dict."""
        try:
            encoded = urllib.request.quote(url, safe="/:?=&#")
            req = urllib.request.Request(
                f"{self._base}/json/new?{encoded}", method="PUT"
            )
            with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.URLError as exc:
            raise CDPError(f"Failed to create tab: {exc}") from exc

    def close_tab(self, tab_id: str) -> None:
        """Close a tab by ID."""
        try:
            req = urllib.request.Request(
                f"{self._base}/json/close/{tab_id}", method="PUT"
            )
            with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as r:
                r.read()
        except urllib.error.URLError as exc:
            raise CDPError(f"Failed to close tab {tab_id!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # Browser info / lifecycle
    # ------------------------------------------------------------------

    def browser_version(self) -> dict:
        """Return Chrome version info, or raise CDPError if not reachable."""
        try:
            with urllib.request.urlopen(
                f"{self._base}/json/version", timeout=_CONNECT_TIMEOUT
            ) as r:
                return json.loads(r.read())
        except urllib.error.URLError as exc:
            raise CDPError(
                f"Chrome not reachable at localhost:{self.port}"
            ) from exc

    @staticmethod
    def kill_chrome(port: int) -> str:
        """Kill Chrome process listening on *port*.  Returns status message."""
        import platform
        import subprocess
        system = platform.system()

        if system == "Windows":
            # Windows: use netstat + taskkill
            try:
                out = subprocess.check_output(
                    ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
                )
                pids = set()
                for line in out.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.split()
                        if parts:
                            try:
                                pids.add(int(parts[-1]))
                            except ValueError:
                                pass
                if not pids:
                    return f"No process found on port {port}"
                for pid in pids:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                    )
                return f"Killed PID(s): {', '.join(str(p) for p in sorted(pids))}"
            except (subprocess.CalledProcessError, FileNotFoundError):
                return f"No process found on port {port}"
        else:
            # macOS / Linux: use lsof + SIGTERM
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f"tcp:{port}"], text=True
                ).strip()
            except (subprocess.CalledProcessError, FileNotFoundError):
                return f"No process found on port {port}"
            pids = {int(p) for p in out.split() if p.strip()}
            if not pids:
                return f"No process found on port {port}"
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            return f"Sent SIGTERM to PID(s): {', '.join(str(p) for p in sorted(pids))}"

    # ------------------------------------------------------------------
    # Extra CDP helpers
    # ------------------------------------------------------------------

    def set_file(self, tab_id: str, selector: str, files: list[str]) -> None:
        """Set files on a file input element."""
        doc = self.send(tab_id, "DOM.getDocument", {})
        root_id = doc["root"]["nodeId"]
        node = self.send(tab_id, "DOM.querySelector", {
            "nodeId": root_id,
            "selector": selector,
        })
        node_id = node.get("nodeId", 0)
        if not node_id:
            raise CDPError(f"Element not found: {selector!r}")
        self.send(tab_id, "DOM.setFileInputFiles", {
            "nodeId": node_id,
            "files": files,
        })

    def submit_form(self, tab_id: str, selector: str | None = None) -> Any:
        """Submit a form by selector or the first form on page."""
        sel = json.dumps(selector) if selector else "'form'"
        expr = f"""
        (function() {{
            var form = document.querySelector({sel});
            if (!form) return {{error: "No form found"}};
            if (form.tagName !== 'FORM') form = form.closest('form');
            if (!form) return {{error: "Element is not inside a form"}};
            var btn = form.querySelector('[type=submit],button:not([type])');
            if (btn) {{ btn.click(); return {{method: "click", tag: btn.tagName}}; }}
            form.submit();
            return {{method: "submit"}};
        }})()
        """
        result = self.js_eval(tab_id, expr)
        if isinstance(result, dict) and "error" in result:
            raise CDPError(result["error"])
        return result

    def send_raw(self, tab_id: str, method: str, params: dict | None = None,
                 timeout: float = _CMD_TIMEOUT) -> dict:
        """Send a raw CDP command and return the full result."""
        return self.send(tab_id, method, params, timeout=timeout)
