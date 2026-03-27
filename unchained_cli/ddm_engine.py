"""DOM Density Map — Text-based page layout for LLM consumption.

Connects to local Chrome via CDP, walks the visible DOM, and renders
a character grid showing element density + type hints. Interactive
elements are indexed with labels and coordinates.

Usage:
    ddm                              # Map current page (first tab)
    ddm <url>                        # Navigate + map
    ddm --cols 120                   # Custom grid width
    ddm --blocks                     # Unicode block art mode
    ddm --at 694,584                 # Reverse lookup at pixel coords
    ddm --at g48,40                  # Reverse lookup at grid coords (prefix 'g')
    ddm --sparse                     # RLE + row dedup (minimal tokens)
    ddm --interactive                # Interactive elements only (smallest output)
    ddm --text                       # Extract page text (innerText)
    ddm --text --find "price"        # Find keyword in page text
    ddm --forms                      # Detect forms as callable tool contracts
    ddm --json                       # Structured JSON output

Tab management:
    ddm --tabs                       # List page/popup tabs with IDs and URLs
    ddm --tab <id>                   # Use a specific tab by ID
    ddm --new ...                    # Create a new tab and use it
    ddm --new <url> ...              # Create a new tab, navigate, and map
    ddm --close <id>                 # Close a tab by ID
"""

import asyncio
import base64
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

import websockets

# ---------------------------------------------------------------------------
# Module constants (extracted from inline magic numbers)
# ---------------------------------------------------------------------------
MAX_DOM_ELEMENTS = 2000       # JS DOM walker cap
MAX_INTERACTIVE = 50          # Interactive element list cap
MAX_GRID_CELLS = 16000        # Grid cell cap to prevent OOM
DEFAULT_COLS = 160            # Default grid width
LABEL_TRUNC_SHORT = 25        # _label_short default max length
Y_BAND_THRESHOLD = 50         # render_llm_group band grouping distance (px)
DEFAULT_TEXT_MAX = 3000       # --text default char limit
FIND_NEARBY_PX = 100          # --find nearby interactive search radius (px)

# ---------------------------------------------------------------------------
# Standalone CDP shim — replaces private-core cdp.py imports
# ---------------------------------------------------------------------------
_CDP_HOST = "127.0.0.1"
_CDP_PORT = 9222  # overridden by --port


class CDP:
    """Minimal async CDP client over WebSocket (local Chrome only)."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self._id = 0

    async def connect(self, timeout: float = 15):
        self.ws = await asyncio.wait_for(
            websockets.connect(self.ws_url, max_size=50 * 1024 * 1024, ping_timeout=None),
            timeout=timeout,
        )
        await self.send("Page.enable")

    async def send(self, method: str, params: dict = None, timeout: float = 30) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if resp.get("method") == "Page.javascriptDialogOpening":
                self._id += 1
                await self.ws.send(json.dumps({
                    "id": self._id,
                    "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": True},
                }))
                continue
            if resp.get("id") == msg["id"]:
                return resp.get("result", {})

    async def navigate(self, url: str, wait: float = 5):
        await self.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": "window.addEventListener('beforeunload', function(e) { e.stopImmediatePropagation(); }, true);"
        })
        self._id += 1
        nav_id = self._id
        await self.ws.send(json.dumps({
            "id": nav_id, "method": "Page.navigate", "params": {"url": url}
        }))
        nav_done = False
        load_fired = False
        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                resp = json.loads(await asyncio.wait_for(
                    self.ws.recv(), timeout=min(remaining, 1.0)))
            except asyncio.TimeoutError:
                if nav_done:
                    break
                continue
            if resp.get("method") == "Page.javascriptDialogOpening":
                self._id += 1
                await self.ws.send(json.dumps({
                    "id": self._id,
                    "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": True},
                }))
                continue
            if resp.get("id") == nav_id:
                nav_done = True
                if load_fired:
                    break
                continue
            if resp.get("method", "") in ("Page.loadEventFired", "Page.frameStoppedLoading"):
                load_fired = True
                if nav_done:
                    break
        await self._wait_dom_stable()

    async def _wait_dom_stable(self, settle: float = 0.25, budget: float = 3.0):
        poll_interval = 0.12
        loop = asyncio.get_event_loop()
        deadline = loop.time() + budget
        prev_count = -1
        stable_since = loop.time()
        while loop.time() < deadline:
            try:
                result = await asyncio.wait_for(
                    self.send("Runtime.evaluate", {
                        "expression": "document.querySelectorAll('*').length",
                        "returnByValue": True,
                    }), timeout=2.0)
                count = result.get("result", {}).get("value", 0)
            except Exception:
                count = -2
            now = loop.time()
            if count != prev_count:
                prev_count = count
                stable_since = now
            elif now - stable_since >= settle:
                return
            remaining = deadline - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

    async def execute_js(self, expression: str) -> dict:
        return await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })

    async def get_page_info(self) -> tuple:
        result = await self.execute_js(
            "JSON.stringify({t:document.title,u:window.location.href})")
        raw = result.get("result", {}).get("value", "{}")
        info = json.loads(raw)
        return info.get("t", ""), info.get("u", "")

    async def get_text(self, max_len: int = 5000) -> str:
        result = await self.send("Runtime.evaluate", {
            "expression": f"document.body.innerText.substring(0, {max_len})"
        })
        return result.get("result", {}).get("value", "")

    async def is_pdf(self) -> bool:
        result = await self.execute_js("document.contentType")
        ct = result.get("result", {}).get("value", "")
        return ct == "application/pdf"

    async def fetch_pdf_base64(self, url: str | None = None) -> str:
        url_arg = f'"{url}"' if url else "null"
        js = """
        (async () => {
            const url = %s || window.location.href;
            const r = await fetch(url, {credentials: 'include'});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            const chunks = [];
            for (let i = 0; i < bytes.length; i += 8192) {
                chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, Math.min(i + 8192, bytes.length))));
            }
            return btoa(chunks.join(''));
        })()
        """ % url_arg
        result = await self.send("Runtime.evaluate", {
            "expression": js, "returnByValue": True, "awaitPromise": True,
        }, timeout=60)
        val = result.get("result", {}).get("value", "")
        if not val:
            raise RuntimeError("PDF fetch returned empty")
        return val

    async def batch_evaluate(self, expressions: list, timeout: float = 30) -> list:
        ids = []
        for expr in expressions:
            self._id += 1
            ids.append(self._id)
            await self.ws.send(json.dumps({
                "id": self._id, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True},
            }))
        results = {}
        pending = set(ids)
        while pending:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if resp.get("method") == "Page.javascriptDialogOpening":
                self._id += 1
                await self.ws.send(json.dumps({
                    "id": self._id,
                    "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": True},
                }))
                continue
            resp_id = resp.get("id")
            if resp_id in pending:
                if "exceptionDetails" in resp:
                    exc = resp["exceptionDetails"].get("exception", {})
                    desc = exc.get("description", exc.get("value", "unknown error"))
                    results[resp_id] = {
                        "result": {"type": "error", "value": None},
                        "_error": str(desc),
                    }
                else:
                    results[resp_id] = resp.get("result", {})
                pending.discard(resp_id)
        return [results[i] for i in ids]


def _list_tabs() -> list[dict]:
    """Return page and popup tabs from Chrome."""
    req = urllib.request.Request(f"http://{_CDP_HOST}:{_CDP_PORT}/json")
    with urllib.request.urlopen(req, timeout=3) as resp:
        tabs = json.loads(resp.read())
    return [t for t in tabs if t.get("type") in ("page", "popup")]


def _get_ws_url_for_tab(tab_id: str) -> str:
    """Get websocket URL for a specific tab by ID (prefix match OK)."""
    tabs = _list_tabs()
    matches = [t for t in tabs if t["id"].startswith(tab_id)]
    if len(matches) == 1:
        return matches[0]["webSocketDebuggerUrl"]
    elif len(matches) == 0:
        raise RuntimeError(f"Tab {tab_id} not found. Use --tabs to list available tabs.")
    else:
        ids = ", ".join(m["id"][:12] for m in matches)
        raise RuntimeError(f"Tab prefix '{tab_id}' is ambiguous ({len(matches)} matches: {ids}).")


def _create_new_tab(url: str = "about:blank") -> dict:
    """Create a new tab and return its info dict."""
    encoded = urllib.parse.quote(url, safe=':/?#[]@!$&\'()*+,;=-._~')
    req = urllib.request.Request(
        f"http://{_CDP_HOST}:{_CDP_PORT}/json/new?{encoded}", method="PUT"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _close_tab(tab_id: str) -> bool:
    """Close a tab by ID."""
    req = urllib.request.Request(
        f"http://{_CDP_HOST}:{_CDP_PORT}/json/close/{tab_id}", method="PUT"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().strip() == b"Target is closing"


def _ensure_chrome():
    """Check Chrome is reachable on the configured port."""
    try:
        req = urllib.request.Request(f"http://{_CDP_HOST}:{_CDP_PORT}/json/version")
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
    except Exception:
        print(f"Chrome not reachable at {_CDP_HOST}:{_CDP_PORT}.", file=sys.stderr)
        print(f"Launch with: unchained launch --port {_CDP_PORT}", file=sys.stderr)
        sys.exit(1)


def _get_ws_url(tab_id: str | None = None) -> str:
    """Get websocket URL for a tab."""
    if tab_id:
        return _get_ws_url_for_tab(tab_id)
    tabs = _list_tabs()
    page_tabs = [t for t in tabs if t.get("type") == "page"]
    if page_tabs:
        return page_tabs[0]["webSocketDebuggerUrl"]
    if tabs:
        return tabs[0]["webSocketDebuggerUrl"]
    raise RuntimeError("No page tab found")

# ---------------------------------------------------------------------------
# Stage 1: JavaScript DOM walker (runs in browser via Runtime.evaluate)
# ---------------------------------------------------------------------------

DOM_WALKER_JS = r"""
(function() {
    var vw = window.innerWidth, vh = window.innerHeight;
    // Recursive collector that enters open shadow roots
    function _collectAll(root, out) {
        var children = root.querySelectorAll('*');
        for (var i = 0; i < children.length; i++) {
            if (children[i].hasAttribute && children[i].hasAttribute('data-unchained-overlay')) continue;
            out.push(children[i]);
            try {
                var sr = children[i].shadowRoot;
                if (sr) _collectAll(sr, out);
            } catch(e) {}  // closed shadow roots throw
        }
    }
    var all = [];
    _collectAll(document, all);
    var elems = [];

    // Saliency test — reject code identifiers as labels
    function _isSalient(s) {
        if (!s || s.length <= 1) return false;
        if (/^[a-z][a-zA-Z0-9]*[A-Z]/.test(s)) return false;  // camelCase
        if (/^[a-z]+[_-][a-z]/.test(s)) return false;          // snake_case, kebab-case
        if (/^\d+$/.test(s)) return false;                      // pure digits
        return true;
    }

    // Context probe — look outward from element for human-readable label
    function _probeContext(el) {
        // Adjacent sibling text (next, then previous)
        var sib = el.nextSibling;
        if (sib) {
            var st = (sib.textContent || '').trim();
            if (st.length > 1 && st.length <= 60) return st;
        }
        sib = el.previousSibling;
        if (sib) {
            var st = (sib.textContent || '').trim();
            if (st.length > 1 && st.length <= 60) return st;
        }
        // Fieldset legend
        var fs = el.closest('fieldset');
        if (fs) {
            var legend = fs.querySelector('legend');
            if (legend) {
                var lt = legend.textContent.trim();
                if (lt) return lt;
            }
        }
        // Table column header
        var td = el.closest('td, th');
        if (td) {
            var ci = 0, prev = td;
            while ((prev = prev.previousElementSibling)) ci++;
            var table = el.closest('table');
            if (table) {
                var hrow = table.querySelector('thead tr') || table.querySelector('tr');
                if (hrow) {
                    var hcells = hrow.children;
                    if (ci < hcells.length) {
                        var ht = hcells[ci].textContent.trim();
                        if (ht) return ht;
                    }
                }
            }
        }
        // Preceding sibling element text
        var pe = el.previousElementSibling;
        if (pe) {
            var pt = pe.textContent.trim();
            if (pt.length > 1 && pt.length <= 40) return pt;
        }
        return '';
    }
    var CAP = 2000;
    var shadowHosts = 0;

    // Layout region thresholds (fraction of viewport)
    // HEADER_ZONE: top 12% — nav bars, search headers
    // SIDEBAR_EDGE: left/right 15% — sidebars, filters, nav columns
    var topY = vh * 0.12, leftX = vw * 0.15, rightX = vw * 0.85;
    var reg = {
        topN:0,topNav:0, leftN:0,leftNav:0, rightN:0,rightNav:0,
        cN:0,cImg:0,cText:0,cLink:0
    };

    for (var i = 0; i < all.length && elems.length < CAP; i++) {
        var el = all[i];
        if (el.shadowRoot) shadowHosts++;
        var st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        if (parseFloat(st.opacity) === 0) continue;

        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) continue;

        // Clamp to viewport
        var x = Math.max(0, r.left);
        var y = Math.max(0, r.top);
        var w = Math.min(r.right, vw) - x;
        var h = Math.min(r.bottom, vh) - y;
        if (w <= 0 || h <= 0) continue;

        // Classify element type (first match)
        var tag = el.tagName.toLowerCase();
        var role = el.getAttribute('role');
        var k = null;  // kind
        var isInteractive = false;

        if (tag === 'button' || role === 'button' ||
            role === 'menuitem' || role === 'option' || role === 'tab' ||
            role === 'switch' || role === 'checkbox' || role === 'radio' ||
            (tag === 'input' && (el.type === 'button' || el.type === 'submit' || el.type === 'reset'))) {
            k = 'B'; isInteractive = true;
        } else if (tag === 'input' || tag === 'textarea' || tag === 'select' ||
                   el.contentEditable === 'true' || role === 'textbox' ||
                   role === 'combobox' || role === 'searchbox' || role === 'spinbutton') {
            k = 'F'; isInteractive = true;
        } else if ((tag === 'a' && el.href) || role === 'link') {
            k = 'L'; isInteractive = true;
        } else if (tag === 'canvas') {
            k = 'C';
        } else if (tag === 'iframe') {
            k = 'IF';
        } else if (tag === 'img' || tag === 'video' || tag === 'svg') {
            k = 'I';
        } else {
            // Check for text content (direct text nodes only)
            var directText = '';
            for (var c = 0; c < el.childNodes.length; c++) {
                if (el.childNodes[c].nodeType === 3) {
                    directText += el.childNodes[c].textContent;
                }
            }
            if (directText.trim().length > 20) {
                k = 'T';
            }
        }

        var entry = {x: Math.round(x), y: Math.round(y),
                     w: Math.round(w), h: Math.round(h)};
        if (k) entry.k = k;

        // Capture iframe src domain
        if (k === 'IF') {
            try {
                var src = el.src || '';
                if (src) {
                    var d = new URL(src).hostname.replace('www.', '');
                    if (d) entry.d = d;
                }
            } catch(e) {}
        }

        // Capture label for interactive elements — 3-phase resolution:
        //   Phase 1: Standard label sources (aria, <label>, placeholder, textContent)
        //   Phase 2: Saliency gate — reject code identifiers (camelCase, snake_case)
        //   Phase 3: Context probe — look outward (siblings, fieldset, table headers)
        if (isInteractive) {
            entry.i = true;

            // --- Phase 1: Standard label sources ---
            var label = el.getAttribute('aria-label') || '';
            if (!label) {
                var lblBy = el.getAttribute('aria-labelledby');
                if (lblBy) {
                    var ids = lblBy.split(/\s+/), lparts = [];
                    for (var li = 0; li < ids.length; li++) {
                        var ref = document.getElementById(ids[li]);
                        if (ref) lparts.push(ref.textContent.trim());
                    }
                    if (lparts.length) label = lparts.join(' ');
                }
            }
            if (!label) {
                var eid = el.id;
                if (eid) {
                    var labelEl = document.querySelector('label[for="' + CSS.escape(eid) + '"]');
                    if (labelEl) label = labelEl.textContent.trim();
                }
            }
            if (!label) {
                var wrapLabel = el.closest('label');
                if (wrapLabel) {
                    var lt = wrapLabel.textContent || '', et = el.textContent || '';
                    label = (et ? lt.replace(et, '') : lt).trim();
                    if (!label) label = lt.trim();
                }
            }
            if (!label && tag === 'select' && el.selectedIndex >= 0) {
                var selOpt = el.options[el.selectedIndex];
                if (selOpt) label = selOpt.textContent.trim();
            }
            if (!label) label = el.placeholder || '';
            if (!label && tag !== 'select') {
                label = (el.textContent || '').trim().substring(0, 60);
            }
            if (!label) label = el.title || '';

            // --- Phase 2: Saliency gate ---
            if (!label || !_isSalient(label)) {
                // --- Phase 3: Context probe ---
                var ctx = _probeContext(el);
                if (ctx) {
                    label = ctx;
                } else if (!label && el.name) {
                    // Typed fallback — prefix with type for minimum disambiguation
                    var pfx = tag === 'select' ? 'select' : (el.type || tag);
                    label = pfx + ':' + el.name;
                }
            }

            label = label.replace(/\s+/g, ' ').trim();
            if (label.length > 60) label = label.substring(0, 57) + '...';
            if (label) entry.l = label;
            // Extra metadata for form inputs
            if (tag === 'input' || tag === 'textarea' || tag === 'select') {
                if (el.type && el.type !== 'text') entry.it = el.type;
                if (el.name) entry.n = el.name;
            }
        }

        elems.push(entry);

        // Accumulate layout region counts (center of element)
        var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        var isNav = isInteractive || tag === 'a';
        if (cy < topY) {
            reg.topN++; if (isNav) reg.topNav++;
        } else if (cx < leftX) {
            reg.leftN++; if (isNav) reg.leftNav++;
        } else if (cx > rightX) {
            reg.rightN++; if (isNav) reg.rightNav++;
        } else {
            reg.cN++;
            if (tag === 'img') reg.cImg++;
            else if (tag === 'a') reg.cLink++;
            else if (k === 'T') reg.cText++;
        }
    }

    // Build layout summary from region accumulators
    var layoutParts = [];
    if (reg.topN > 3 && reg.topNav > 1) layoutParts.push('header');
    if (reg.leftN > 10) layoutParts.push('sidebar(left)');
    if (reg.rightN > 10) layoutParts.push('sidebar(right)');
    var cType = 'mixed';
    if (reg.cImg > 5 && reg.cImg >= reg.cText) cType = 'cards';
    else if (reg.cText > reg.cImg && reg.cText > 5) cType = 'text';
    else if (reg.cLink > reg.cText && reg.cLink > 5) cType = 'links';
    layoutParts.push('content(' + cType + ')');
    // Scroll height used for both layout summary and return value
    var _sh = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
    if (_sh > vh * 3) layoutParts.push('scroll(' + Math.round(_sh / vh) + 'x)');
    var layoutSummary = layoutParts.join(' | ');

    // Overlay/modal detection — two passes:
    // Pass 1: semantic (role/class/tag patterns)
    // Pass 2: behavioral (position:fixed covering >40% viewport, regardless of class)
    var overlayInfo = null;

    function _captureOverlay(ov) {
        var ovSt = window.getComputedStyle(ov);
        var ovTag = ov.tagName.toLowerCase();
        var ovRole = ov.getAttribute('role') || '';
        var ovClass = (ov.className && typeof ov.className === 'string')
                      ? ov.className.trim().split(/\s+/).slice(0, 3).join(' ') : '';
        var ovR = ov.getBoundingClientRect();
        var ovBtns = [];
        var btns = ov.querySelectorAll('button, [role="button"], input[type="submit"], a[href]');
        for (var b = 0; b < btns.length && ovBtns.length < 5; b++) {
            var btnText = (btns[b].textContent || btns[b].getAttribute('aria-label') || '').trim();
            if (btnText && btnText.length < 40) ovBtns.push(btnText);
        }
        var ovFields = [];
        var flds = ov.querySelectorAll('input, textarea, select');
        for (var f = 0; f < flds.length && ovFields.length < 5; f++) {
            var fLabel = flds[f].getAttribute('aria-label') || flds[f].placeholder
                         || flds[f].name || flds[f].type || '';
            if (fLabel) ovFields.push(fLabel);
        }
        return {
            tag: ovTag, role: ovRole, cls: ovClass,
            rect: {x: Math.round(ovR.x), y: Math.round(ovR.y),
                   w: Math.round(ovR.width), h: Math.round(ovR.height)},
            zIndex: parseInt(ovSt.zIndex) || 0, position: ovSt.position,
            buttons: ovBtns, fields: ovFields
        };
    }

    // Pass 1: semantic selectors (role, aria-modal, class patterns, dialog tag)
    var candidates = document.querySelectorAll(
        '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open], ' +
        '[class*="modal" i], [class*="overlay" i], [class*="popup" i], [class*="drawer" i]'
    );
    for (var j = 0; j < candidates.length && !overlayInfo; j++) {
        var ov = candidates[j];
        var ovSt = window.getComputedStyle(ov);
        if (ovSt.display === 'none' || ovSt.visibility === 'hidden') continue;
        if (parseFloat(ovSt.opacity) === 0) continue;
        var ovR = ov.getBoundingClientRect();
        if (ovR.width < 100 || ovR.height < 100) continue;
        var pos = ovSt.position;
        var zi = parseInt(ovSt.zIndex) || 0;
        var isPositioned = (pos === 'fixed' || pos === 'absolute') && zi > 10;
        var isDialog = ov.tagName === 'DIALOG' || ov.getAttribute('role') === 'dialog'
                       || ov.getAttribute('role') === 'alertdialog'
                       || ov.getAttribute('aria-modal') === 'true';
        if (!isPositioned && !isDialog) continue;
        overlayInfo = _captureOverlay(ov);
    }

    // Pass 2: behavioral — position:fixed/absolute covering >20% of viewport
    // Catches obfuscated-class overlays and cookie banners
    if (!overlayInfo) {
        var vpArea = vw * vh;
        var allEls = document.querySelectorAll('*');
        var bestOv = null, bestZi = -1;
        for (var j = 0; j < allEls.length && j < 500; j++) {
            var ov = allEls[j];
            var ovSt = window.getComputedStyle(ov);
            if (ovSt.display === 'none' || ovSt.visibility === 'hidden') continue;
            var pos = ovSt.position;
            if (pos !== 'fixed' && pos !== 'absolute') continue;
            var zi = parseInt(ovSt.zIndex) || 0;
            if (zi < 100) continue;
            var ovR = ov.getBoundingClientRect();
            var ovArea = ovR.width * ovR.height;
            if (ovArea < vpArea * 0.2) continue;
            // Skip body/html/main structural elements
            var tag = ov.tagName.toLowerCase();
            if (tag === 'html' || tag === 'body' || tag === 'main' || tag === 'header' || tag === 'nav') continue;
            if (zi > bestZi) {
                bestOv = ov;
                bestZi = zi;
            }
        }
        if (bestOv) {
            overlayInfo = _captureOverlay(bestOv);
        }
    }

    return {vw: vw, vh: vh, sy: Math.round(window.scrollY), sh: _sh, count: elems.length, elements: elems, shadow: shadowHosts, overlay: overlayInfo, layout: layoutSummary};
})()
"""

ELEMENTS_AT_JS = r"""
(function(px, py) {
    var els = document.elementsFromPoint(px, py);
    var results = [];
    for (var i = 0; i < els.length && results.length < 15; i++) {
        var el = els[i];
        var tag = el.tagName.toLowerCase();
        var r = el.getBoundingClientRect();
        var entry = {
            tag: tag,
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)}
        };

        // Attributes worth showing
        if (el.id) entry.id = el.id;
        var cls = el.className;
        if (typeof cls === 'string' && cls.trim()) {
            entry.cls = cls.trim().split(/\s+/).slice(0, 4).join(' ');
        }
        var role = el.getAttribute('role');
        if (role) entry.role = role;
        var de = el.getAttribute('data-e2e');
        if (de) entry.data_e2e = de;
        var aria = el.getAttribute('aria-label');
        if (aria) entry.aria = aria.substring(0, 80);
        var href = el.getAttribute('href');
        if (href) entry.href = href.substring(0, 120);

        // State
        if (el.getAttribute('aria-pressed')) entry.pressed = el.getAttribute('aria-pressed');
        if (el.contentEditable === 'true') entry.editable = true;
        if (el.disabled) entry.disabled = true;

        // Direct text (first 80 chars)
        var txt = '';
        for (var c = 0; c < el.childNodes.length; c++) {
            if (el.childNodes[c].nodeType === 3) txt += el.childNodes[c].textContent;
        }
        txt = txt.trim();
        if (txt) entry.text = txt.substring(0, 80);

        // Computed style hints
        var st = window.getComputedStyle(el);
        if (st.cursor === 'pointer') entry.clickable = true;
        var bg = st.backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') entry.bg = bg;
        var color = st.color;
        if (color) entry.color = color;

        results.push(entry);
    }
    return results;
})(%d, %d)
"""


TEXT_FIND_JS = r"""
(function(keyword, maxResults) {
    var kw = keyword.toLowerCase();
    var results = [];
    var vh = window.innerHeight;
    var walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null, false
    );
    var seen = new Set();
    var node;
    while ((node = walker.nextNode()) && results.length < maxResults) {
        var text = node.textContent;
        if (!text || text.toLowerCase().indexOf(kw) === -1) continue;
        var parent = node.parentElement;
        if (!parent || seen.has(parent)) continue;
        seen.add(parent);
        var r = parent.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        var cx = Math.round(r.left + r.width / 2);
        var cy = Math.round(r.top + r.height / 2);
        // Context: up to 120 chars around keyword
        var idx = text.toLowerCase().indexOf(kw);
        var start = Math.max(0, idx - 40);
        var end = Math.min(text.length, idx + kw.length + 80);
        var ctx = (start > 0 ? '...' : '') +
                  text.substring(start, end).trim() +
                  (end < text.length ? '...' : '');
        results.push({
            px: cx, py: cy,
            tag: parent.tagName.toLowerCase(),
            ctx: ctx,
            below: r.top >= vh
        });
    }
    return results;
})(%s, %d)
"""


FORMS_JS = r"""
(function() {
    var forms = document.querySelectorAll('form');
    var result = [];

    // Also detect "implicit forms" — groups of inputs not inside a <form>
    var allInputs = document.querySelectorAll('input, textarea, select');
    var orphanInputs = [];
    for (var i = 0; i < allInputs.length; i++) {
        if (!allInputs[i].closest('form')) orphanInputs.push(allInputs[i]);
    }

    function describeField(el) {
        var tag = el.tagName.toLowerCase();
        var field = {tag: tag};
        if (el.name) field.name = el.name;
        if (el.id) field.id = el.id;
        if (tag === 'input') field.type = el.type || 'text';
        if (el.placeholder) field.placeholder = el.placeholder;
        if (el.required) field.required = true;
        if (el.min) field.min = el.min;
        if (el.max) field.max = el.max;
        if (el.pattern) field.pattern = el.pattern;
        if (el.maxLength > 0 && el.maxLength < 524288) field.maxLength = el.maxLength;

        // Resolve label
        var label = el.getAttribute('aria-label') || '';
        if (!label && el.id) {
            var lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) label = lbl.textContent.trim();
        }
        if (!label) label = el.title || el.placeholder || '';
        if (label) field.label = label.substring(0, 80);

        // Current value (truncated)
        if (el.value) field.value = el.value.substring(0, 50);

        // Select options
        if (tag === 'select') {
            var opts = [];
            for (var j = 0; j < el.options.length && j < 20; j++) {
                opts.push({v: el.options[j].value, t: el.options[j].text.trim().substring(0, 40)});
            }
            field.options = opts;
        }

        // Hidden inputs (CSRF tokens, viewstate)
        if (el.type === 'hidden') field.hidden = true;

        return field;
    }

    function describeForm(form, index) {
        var f = {index: index};
        if (form.action) f.action = form.action;
        if (form.method) f.method = (form.method || 'GET').toUpperCase();
        if (form.id) f.id = form.id;
        if (form.name) f.name = form.name;

        var fields = [];
        var inputs = form.querySelectorAll('input, textarea, select');
        for (var i = 0; i < inputs.length && i < 30; i++) {
            fields.push(describeField(inputs[i]));
        }
        f.fields = fields;

        // Find submit buttons
        var submits = [];
        var btns = form.querySelectorAll('button[type=submit], input[type=submit], button:not([type])');
        for (var i = 0; i < btns.length; i++) {
            var txt = btns[i].textContent.trim() || btns[i].value || 'Submit';
            submits.push(txt.substring(0, 40));
        }
        f.submits = submits;

        return f;
    }

    // Real forms
    for (var i = 0; i < forms.length; i++) {
        result.push(describeForm(forms[i], i));
    }

    // Orphan inputs (not in any form)
    if (orphanInputs.length > 0) {
        var orphan = {index: result.length, implicit: true, fields: []};
        for (var i = 0; i < orphanInputs.length && i < 30; i++) {
            orphan.fields.push(describeField(orphanInputs[i]));
        }
        result.push(orphan);
    }

    return result;
})()
"""


API_SCAN_JS = r"""
(function(search) {
    var start = performance.now();
    var TIMEOUT = 1500;
    var MAX_RESULTS = 20;
    var MAX_CHECKED_L0 = 500;
    var MAX_KEYS_L1 = 100;
    var MAX_KEYS_L2 = 50;
    var MAX_DEPTH = 3;

    // Skip browser builtins, DOM elements, known framework noise
    var SKIP = new Set([
        'window','self','globalThis','top','parent','frames','document','location',
        'navigator','screen','history','performance','caches','cookieStore',
        'localStorage','sessionStorage','indexedDB','crypto','fetch','alert',
        'confirm','prompt','open','close','stop','focus','blur','print',
        'requestAnimationFrame','cancelAnimationFrame','setTimeout','setInterval',
        'clearTimeout','clearInterval','postMessage','addEventListener',
        'removeEventListener','dispatchEvent','getComputedStyle','matchMedia',
        'getSelection','scrollTo','scrollBy','scroll','moveTo','moveBy',
        'resizeTo','resizeBy','requestIdleCallback','cancelIdleCallback',
        'queueMicrotask','structuredClone','reportError','btoa','atob',
        'createImageBitmap','origin','isSecureContext','crossOriginIsolated',
        'scheduler','trustedTypes','webkitRequestAnimationFrame',
        'webkitCancelAnimationFrame','chrome','onerror','onmessage',
        'onunhandledrejection','speechSynthesis','visualViewport',
        'customElements','external','clientInformation','styleMedia',
        'defaultstatus','defaultStatus','screenLeft','screenTop',
        'screenX','screenY','outerWidth','outerHeight','innerWidth',
        'innerHeight','scrollX','scrollY','pageXOffset','pageYOffset',
        'devicePixelRatio','name','status','closed','toolbar','menubar',
        'personalbar','scrollbars','statusbar','locationbar','frameElement',
        'length','opener','isNaN','isFinite','parseFloat','parseInt',
        'undefined','NaN','Infinity','eval','Array','Object','String',
        'Number','Boolean','Symbol','BigInt','Date','RegExp','Error',
        'TypeError','RangeError','SyntaxError','ReferenceError','URIError',
        'EvalError','Map','Set','WeakMap','WeakSet','Promise','Proxy',
        'Reflect','JSON','Math','console','Intl','ArrayBuffer','SharedArrayBuffer',
        'DataView','Float32Array','Float64Array','Int8Array','Int16Array',
        'Int32Array','Uint8Array','Uint16Array','Uint32Array','Uint8ClampedArray',
        'BigInt64Array','BigUint64Array','Function','GeneratorFunction',
        'AsyncGeneratorFunction','AsyncFunction','Iterator','AsyncIterator',
        'AggregateError','FinalizationRegistry','WeakRef','decodeURI',
        'decodeURIComponent','encodeURI','encodeURIComponent','escape','unescape',
        '__proto__','constructor','hasOwnProperty','toString','valueOf',
        'toLocaleString','propertyIsEnumerable','isPrototypeOf',
        'webkitURL','URL','URLSearchParams','Blob','File','FileReader',
        'FileList','FormData','Headers','Request','Response','ReadableStream',
        'WritableStream','TransformStream','AbortController','AbortSignal',
        'TextEncoder','TextDecoder','Event','CustomEvent','EventTarget',
        'MessageChannel','MessagePort','BroadcastChannel','Worker',
        'SharedWorker','ServiceWorker','WebSocket','XMLHttpRequest',
        'MutationObserver','IntersectionObserver','ResizeObserver',
        'PerformanceObserver','Cache','CacheStorage'
    ]);

    // Known app signatures: {name: [methods to check]}
    var SIGS = {
        mxGraph: ['insertVertex','insertEdge','getModel','getDefaultParent'],
        tldraw: ['createShape','deleteShape','updateShape','getShape'],
        fabric: ['add','remove','renderAll','getObjects','setActiveObject'],
        monaco: ['getModel','getValue','setValue','getPosition','setPosition'],
        redux: ['getState','dispatch','subscribe','replaceReducer'],
        vuex: ['commit','dispatch','getters','state'],
        d3: ['select','selectAll','scaleLinear','scaleOrdinal'],
        threejs: ['add','remove','traverse','updateMatrixWorld'],
        cytoscape: ['add','remove','nodes','edges','layout'],
        leaflet: ['addLayer','setView','getCenter','getZoom'],
        excalidraw: ['updateScene','getSceneElements','getAppState'],
        prosemirror: ['apply','toJSON','doc','selection'],
        codemirror: ['dispatch','update','state','contentDOM'],
        konva: ['add','remove','draw','getStage','find'],
        pixijs: ['addChild','removeChild','render','stage'],
        phaser: ['add','scene','physics','input'],
        ace: ['getValue','setValue','getSession','setTheme']
    };

    var seen = new WeakSet();
    var results = [];

    function checkSig(obj) {
        var matched = null;
        var bestCount = 0;
        for (var sigName in SIGS) {
            var methods = SIGS[sigName];
            var count = 0;
            for (var m = 0; m < methods.length; m++) {
                try {
                    if (typeof obj[methods[m]] === 'function') count++;
                } catch(e) {}
            }
            if (count >= 2 && count > bestCount) {
                matched = sigName;
                bestCount = count;
            }
        }
        return matched ? {name: matched, matched: bestCount, total: SIGS[matched].length} : null;
    }

    function countMethods(obj, maxKeys) {
        var count = 0;
        var names = [];
        try {
            var keys = Object.getOwnPropertyNames(obj);
            for (var k = 0; k < keys.length && k < maxKeys; k++) {
                try {
                    if (typeof obj[keys[k]] === 'function' && keys[k] !== 'constructor') {
                        count++;
                        if (names.length < 8) names.push(keys[k]);
                    }
                } catch(e) {}
            }
        } catch(e) {}
        return {count: count, names: names};
    }

    function isDOMNode(obj) {
        try {
            return obj instanceof Node || obj instanceof Element ||
                   obj instanceof HTMLElement || obj instanceof SVGElement ||
                   (obj.nodeType !== undefined && obj.nodeName !== undefined);
        } catch(e) { return false; }
    }

    function isWindow(obj) {
        try { return obj === window || obj.window === obj; } catch(e) { return false; }
    }

    // Mode 1: method name search — find any global with this method
    if (search && search.indexOf('.') === -1) {
        var props;
        try { props = Object.getOwnPropertyNames(window); } catch(e) { return {mode:'method',search:search,results:[]}; }
        var checked = 0;
        for (var p = 0; p < props.length; p++) {
            if (performance.now() - start > TIMEOUT) break;
            var key = props[p];
            if (SKIP.has(key)) continue;
            try {
                var val = window[key];
                if (val == null || typeof val !== 'object') continue;
                if (isDOMNode(val) || isWindow(val)) continue;
                if (seen.has(val)) continue;
                seen.add(val);
                if (++checked > MAX_CHECKED_L0) break;

                // Check top level
                if (typeof val[search] === 'function') {
                    var mc = countMethods(val, MAX_KEYS_L1);
                    results.push({path: key, type: val.constructor ? val.constructor.name : typeof val, methods: mc.count, sample: mc.names, sig: checkSig(val)});
                    if (results.length >= MAX_RESULTS) break;
                    continue;
                }
                // Check 1 level deep
                var subKeys;
                try { subKeys = Object.getOwnPropertyNames(val); } catch(e) { continue; }
                for (var s = 0; s < subKeys.length && s < MAX_KEYS_L1; s++) {
                    try {
                        var sub = val[subKeys[s]];
                        if (sub == null || typeof sub !== 'object') continue;
                        if (isDOMNode(sub) || isWindow(sub)) continue;
                        if (seen.has(sub)) continue;
                        seen.add(sub);
                        if (typeof sub[search] === 'function') {
                            var mc2 = countMethods(sub, MAX_KEYS_L2);
                            results.push({path: key+'.'+subKeys[s], type: sub.constructor ? sub.constructor.name : typeof sub, methods: mc2.count, sample: mc2.names, sig: checkSig(sub)});
                            if (results.length >= MAX_RESULTS) break;
                        }
                    } catch(e) {}
                }
                if (results.length >= MAX_RESULTS) break;
            } catch(e) {}
        }
        return {mode:'method', search:search, results:results, ms: Math.round(performance.now()-start)};
    }

    // Mode 2: dot-path search — resolve path from each window property
    if (search && search.indexOf('.') !== -1) {
        var pathParts = search.split('.');
        var props;
        try { props = Object.getOwnPropertyNames(window); } catch(e) { return {mode:'dotpath',search:search,results:[]}; }
        var checked = 0;
        for (var p = 0; p < props.length; p++) {
            if (performance.now() - start > TIMEOUT) break;
            var key = props[p];
            if (SKIP.has(key)) continue;
            try {
                var cur = window[key];
                if (cur == null || typeof cur !== 'object') continue;
                if (isDOMNode(cur) || isWindow(cur)) continue;
                if (++checked > MAX_CHECKED_L0) break;

                // Try to resolve the dot-path from this root
                var resolved = cur;
                var ok = true;
                for (var dp = 0; dp < pathParts.length; dp++) {
                    try {
                        resolved = resolved[pathParts[dp]];
                        if (resolved == null) { ok = false; break; }
                    } catch(e) { ok = false; break; }
                }
                if (ok && typeof resolved === 'object') {
                    var mc = countMethods(resolved, MAX_KEYS_L1);
                    if (mc.count > 0) {
                        results.push({path: key+'.'+search, type: resolved.constructor ? resolved.constructor.name : typeof resolved, methods: mc.count, sample: mc.names, sig: checkSig(resolved)});
                        if (results.length >= MAX_RESULTS) break;
                    }
                }
            } catch(e) {}
        }
        return {mode:'dotpath', search:search, results:results, ms: Math.round(performance.now()-start)};
    }

    // Mode 0: full scan — find all API-like objects
    var props;
    try { props = Object.getOwnPropertyNames(window); } catch(e) { return {mode:'scan',results:[]}; }
    var checked = 0;
    for (var p = 0; p < props.length; p++) {
        if (performance.now() - start > TIMEOUT) break;
        var key = props[p];
        if (SKIP.has(key)) continue;
        try {
            var val = window[key];
            if (val == null || typeof val !== 'object') continue;
            if (isDOMNode(val) || isWindow(val)) continue;
            if (seen.has(val)) continue;
            seen.add(val);
            if (++checked > MAX_CHECKED_L0) break;

            var sig = checkSig(val);
            var mc = countMethods(val, MAX_KEYS_L1);

            // Top-level: include if sig match or 3+ methods
            if (sig || mc.count >= 3) {
                results.push({path: key, type: val.constructor ? val.constructor.name : typeof val, methods: mc.count, sample: mc.names, sig: sig, depth: 0});
            }

            // 1 level deep
            var subKeys;
            try { subKeys = Object.getOwnPropertyNames(val); } catch(e) { continue; }
            for (var s = 0; s < subKeys.length && s < MAX_KEYS_L1; s++) {
                if (performance.now() - start > TIMEOUT) break;
                try {
                    var sub = val[subKeys[s]];
                    if (sub == null || typeof sub !== 'object') continue;
                    if (isDOMNode(sub) || isWindow(sub)) continue;
                    if (seen.has(sub)) continue;
                    seen.add(sub);

                    var sig2 = checkSig(sub);
                    var mc2 = countMethods(sub, MAX_KEYS_L2);
                    if (sig2 || mc2.count >= 5) {
                        results.push({path: key+'.'+subKeys[s], type: sub.constructor ? sub.constructor.name : typeof sub, methods: mc2.count, sample: mc2.names, sig: sig2, depth: 1});
                    }

                    // 2 levels deep — only if promising
                    if (mc2.count >= 3) {
                        var subKeys2;
                        try { subKeys2 = Object.getOwnPropertyNames(sub); } catch(e) { continue; }
                        for (var s2 = 0; s2 < subKeys2.length && s2 < MAX_KEYS_L2; s2++) {
                            if (performance.now() - start > TIMEOUT) break;
                            try {
                                var sub2 = sub[subKeys2[s2]];
                                if (sub2 == null || typeof sub2 !== 'object') continue;
                                if (isDOMNode(sub2) || isWindow(sub2)) continue;
                                if (seen.has(sub2)) continue;
                                seen.add(sub2);

                                var sig3 = checkSig(sub2);
                                var mc3 = countMethods(sub2, MAX_KEYS_L2);
                                if (sig3 || mc3.count >= 5) {
                                    results.push({path: key+'.'+subKeys[s]+'.'+subKeys2[s2], type: sub2.constructor ? sub2.constructor.name : typeof sub2, methods: mc3.count, sample: mc3.names, sig: sig3, depth: 2});
                                }
                            } catch(e) {}
                        }
                    }
                } catch(e) {}
            }
        } catch(e) {}
    }

    // Sort: signature matches first (by match count desc), then by method count desc
    results.sort(function(a, b) {
        var sa = a.sig ? a.sig.matched : 0;
        var sb = b.sig ? b.sig.matched : 0;
        if (sb !== sa) return sb - sa;
        return b.methods - a.methods;
    });

    return {mode:'scan', results: results.slice(0, MAX_RESULTS), total_scanned: checked, ms: Math.round(performance.now()-start)};
})(%s)
"""


def render_forms(forms_data, title="", url="", tab=""):
    """Render forms as WebMCP-like tool contracts."""
    lines = []
    lines.append("=== Page Forms (as tool contracts) ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Forms found: {len(forms_data)}")
    lines.append("")

    if not forms_data:
        lines.append("No forms found on this page.")
        return '\n'.join(lines)

    for form in forms_data:
        idx = form.get('index', 0)
        implicit = form.get('implicit', False)
        header = f"Form #{idx}"
        if implicit:
            header += " (implicit — inputs without <form>)"
        if form.get('id'):
            header += f" id=\"{form['id']}\""
        if form.get('name'):
            header += f" name=\"{form['name']}\""
        lines.append(header)

        if form.get('action'):
            lines.append(f"  action: {form['action']}")
        if form.get('method'):
            lines.append(f"  method: {form['method']}")

        fields = form.get('fields', [])
        visible = [f for f in fields if not f.get('hidden')]
        hidden = [f for f in fields if f.get('hidden')]

        if visible:
            lines.append("  params:")
            for f in visible:
                name = f.get('name') or f.get('id') or '(unnamed)'
                ftype = f.get('type', f.get('tag', '?'))
                req = '*' if f.get('required') else ' '
                label = f.get('label', '')
                label_str = f" — {label}" if label else ''

                constraints = []
                if f.get('min'):
                    constraints.append(f"min={f['min']}")
                if f.get('max'):
                    constraints.append(f"max={f['max']}")
                if f.get('pattern'):
                    constraints.append(f"pattern={f['pattern']}")
                if f.get('maxLength'):
                    constraints.append(f"maxLen={f['maxLength']}")
                if f.get('placeholder'):
                    constraints.append(f"eg: \"{f['placeholder']}\"")
                constraint_str = f" [{', '.join(constraints)}]" if constraints else ''

                # Show select options inline
                if f.get('options'):
                    opts = [o['t'] or o['v'] for o in f['options'][:8]]
                    opt_str = ' | '.join(opts)
                    if len(f['options']) > 8:
                        opt_str += f' ... (+{len(f["options"]) - 8})'
                    constraint_str += f" ({opt_str})"

                val = f.get('value', '')
                val_str = f" =\"{val}\"" if val else ''

                lines.append(f"    {req} {name}: {ftype}{label_str}{constraint_str}{val_str}")

        if hidden:
            names = [h.get('name', '?') for h in hidden]
            lines.append(f"  hidden: {', '.join(names)}")

        submits = form.get('submits', [])
        if submits:
            lines.append(f"  submit: {' | '.join(submits)}")

        lines.append("")

    return '\n'.join(lines)


def render_elements_at(elements, px, py, tab=""):
    """Format the elementsFromPoint result as readable text."""
    lines = []
    lines.append(f"=== Elements at px({px},{py}) ===")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Stack depth: {len(elements)}")
    lines.append("")
    for i, el in enumerate(elements):
        tag = el['tag']
        parts = [f"[{i}] <{tag}>"]
        if el.get('id'):
            parts.append(f'id="{el["id"]}"')
        if el.get('role'):
            parts.append(f'role="{el["role"]}"')
        if el.get('data_e2e'):
            parts.append(f'data-e2e="{el["data_e2e"]}"')
        lines.append(' '.join(parts))

        if el.get('cls'):
            lines.append(f"     class: {el['cls']}")
        if el.get('aria'):
            lines.append(f"     aria-label: \"{el['aria']}\"")
        if el.get('href'):
            lines.append(f"     href: {el['href']}")
        if el.get('text'):
            lines.append(f"     text: \"{el['text']}\"")
        if el.get('pressed'):
            lines.append(f"     aria-pressed: {el['pressed']}")
        if el.get('editable'):
            lines.append(f"     contentEditable: true")
        if el.get('disabled'):
            lines.append(f"     disabled: true")
        if el.get('clickable'):
            lines.append(f"     cursor: pointer")
        if el.get('bg'):
            lines.append(f"     bg: {el['bg']}")
        if el.get('color'):
            lines.append(f"     color: {el['color']}")
        r = el.get('rect', {})
        lines.append(f"     rect: ({r.get('x',0)},{r.get('y',0)}) {r.get('w',0)}x{r.get('h',0)}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Stage 2 & 3: Python grid renderer + interactive element index
# ---------------------------------------------------------------------------

# Type priority: higher wins cell ownership
_PRIORITY = {'T': 1, 'I': 2, 'C': 2, 'IF': 2, 'L': 3, 'F': 4, 'B': 5}
# Density chars (when no type override)
_DENSITY_CHARS = {0: ' ', 1: '.', 2: ':', 3: ':', 4: '#', 5: '#', 6: '#', 7: '#'}


_BLOCK_DENSITY = {0: ' ', 1: '░', 2: '▒', 3: '▒', 4: '▓', 5: '▓', 6: '▓', 7: '▓'}
_BLOCK_TYPES = {'B': '▣', 'F': '▤', 'L': '▨', 'I': '▧', 'T': '▥', 'C': '▦', 'IF': '▩'}
# Single-char mapping for text-mode grid (IF is 2 chars — map to X to avoid misalignment)
_KIND_CHAR = {'B': 'B', 'F': 'F', 'L': 'L', 'I': 'I', 'T': 'T', 'C': 'C', 'IF': 'X'}


def _density_char(count, blocks=False):
    if blocks:
        if count == 0:
            return ' '
        if count == 1:
            return '░'
        if count <= 3:
            return '▒'
        if count <= 7:
            return '▓'
        return '█'
    if count == 0:
        return ' '
    if count == 1:
        return '.'
    if count <= 3:
        return ':'
    if count <= 7:
        return '#'
    return '@'


def _build_grid(data, max_cols=DEFAULT_COLS):
    """Build density/kinds grids + interactive list from DOM walker data.

    Returns (density, kinds, interactive, cols, rows, cell_px).
    """
    vw = data['vw']
    vh = data['vh']
    elements = data['elements']

    cols = min(max_cols, vw)
    cell_px = vw / cols
    rows = max(1, round(vh / cell_px))
    if cols * rows > MAX_GRID_CELLS:
        rows = MAX_GRID_CELLS // cols

    density = [[0] * cols for _ in range(rows)]
    kinds = [[None] * cols for _ in range(rows)]
    interactive = []

    for el in elements:
        ex, ey, ew, eh = el['x'], el['y'], el['w'], el['h']
        kind = el.get('k')
        is_interactive = el.get('i', False)

        c0 = max(0, min(int(ex / cell_px), cols - 1))
        c1 = max(0, min(int((ex + ew - 1) / cell_px), cols - 1))
        r0 = max(0, min(int(ey / cell_px), rows - 1))
        r1 = max(0, min(int((ey + eh - 1) / cell_px), rows - 1))

        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                density[r][c] += 1
                if kind:
                    cur = kinds[r][c]
                    if cur is None or _PRIORITY.get(kind, 0) > _PRIORITY.get(cur, 0):
                        kinds[r][c] = kind

        if is_interactive:
            gc = int((ex + ew / 2) / cell_px)
            gr = int((ey + eh / 2) / cell_px)
            interactive.append({
                'kind': kind, 'label': el.get('l', ''),
                'gc': gc, 'gr': gr,
                'px': int(ex + ew / 2), 'py': int(ey + eh / 2),
            })

    interactive.sort(key=lambda e: (e['gr'], e['gc']))
    interactive = interactive[:MAX_INTERACTIVE]
    return density, kinds, interactive, cols, rows, cell_px


def render_density_map(data, title="", url="", max_cols=DEFAULT_COLS, blocks=False, tab=""):
    """Build and return the text density map from DOM walker data."""
    vw, vh = data['vw'], data['vh']
    density, kinds, interactive, cols, rows, cell_px = _build_grid(data, max_cols)
    n_interactive = len(interactive)

    # Build output
    lines = []
    lines.append("=== DOM Density Map ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Viewport: {vw}x{vh}  Grid: {cols}x{rows} ({cell_px:.0f}px/cell)")
    lines.append(f"Elements: {data['count']} visible, {n_interactive} interactive")
    lines.append("")
    if blocks:
        lines.append("Legend: (space)=empty ░=1 ▒=2-3 ▓=4-7 █=8+")
        lines.append("        ▣=button ▨=link ▤=input ▧=image/video ▥=text")
    else:
        lines.append("Legend: (space)=empty .=1elem :=2-3 #=4-7 @=8+")
        lines.append("        B=button L=link F=input I=image/video T=text")
    lines.append("")

    # Column ruler (every 10)
    ruler_tens = ''.join([str((i // 10) % 10) if i % 10 == 0 else ' '
                          for i in range(cols)])
    ruler_ones = ''.join([str(i % 10) for i in range(cols)])
    lines.append(ruler_tens)
    lines.append(ruler_ones)

    # Grid rows
    for r in range(rows):
        row_chars = []
        for c in range(cols):
            k = kinds[r][c]
            d = density[r][c]
            if k:
                row_chars.append(_BLOCK_TYPES[k] if blocks else _KIND_CHAR.get(k, k))
            else:
                row_chars.append(_density_char(d, blocks=blocks))
        lines.append(''.join(row_chars))

    # Interactive element index
    if interactive:
        lines.append("")
        lines.append(f"--- Interactive ({n_interactive}) ---")
        # Assign sequential IDs per type
        type_counters = {}
        for item in interactive:
            k = item['kind'] or '?'
            type_counters[k] = type_counters.get(k, 0) + 1
            eid = f"{k}{type_counters[k]}"
            label = item['label']
            label_str = f' "{label}"' if label else ''
            lines.append(
                f"{eid}:{label_str} at grid({item['gc']},{item['gr']}) "
                f"px({item['px']},{item['py']})"
            )

    return '\n'.join(lines)


def _rle_row(chars):
    """Run-length encode a row of chars. 'BBB@@...' -> 'B3@2.2'"""
    if not chars:
        return ''
    parts = []
    cur = chars[0]
    count = 1
    for ch in chars[1:]:
        if ch == cur:
            count += 1
        else:
            parts.append(cur if count == 1 else f"{cur}{count}")
            cur = ch
            count = 1
    parts.append(cur if count == 1 else f"{cur}{count}")
    return ''.join(parts)


def render_sparse_map(data, title="", url="", max_cols=DEFAULT_COLS, blocks=False, tab=""):
    """Compressed density map: RLE rows + row deduplication."""
    vw, vh = data['vw'], data['vh']
    density, kinds, interactive, cols, rows, cell_px = _build_grid(data, max_cols)

    # Build char rows
    char_rows = []
    for r in range(rows):
        row = []
        for c in range(cols):
            k = kinds[r][c]
            d = density[r][c]
            if k:
                row.append(_BLOCK_TYPES[k] if blocks else _KIND_CHAR.get(k, k))
            else:
                row.append(_density_char(d, blocks=blocks))
        char_rows.append(row)

    # RLE encode each row
    rle_rows = [_rle_row(row) for row in char_rows]

    # Build output
    lines = []
    lines.append(f"=== DOM Density Map (sparse) ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Viewport: {vw}x{vh}  Grid: {cols}x{rows} ({cell_px:.0f}px/cell)")
    lines.append(f"Elements: {data['count']} visible, {len(interactive)} interactive")
    lines.append(f"Key: _=empty .=1 :=2-3 #=4-7 @=8+ B=btn L=link F=input I=img T=text")
    lines.append(f"RLE: X5 = XXXXX")
    lines.append("")

    # Emit rows with dedup (collapse identical consecutive rows)
    i = 0
    while i < len(rle_rows):
        rle = rle_rows[i]
        # Find how many consecutive rows are identical
        j = i + 1
        while j < len(rle_rows) and rle_rows[j] == rle:
            j += 1
        count = j - i

        # Check if row is all empty
        is_empty = all(ch == ' ' for ch in char_rows[i])

        if is_empty:
            if count == 1:
                lines.append(f"r{i}: (empty)")
            else:
                lines.append(f"r{i}-{j-1}: (empty)")
        elif count == 1:
            lines.append(f"r{i}: {rle}")
        elif count == 2:
            lines.append(f"r{i}-{j-1}: {rle}")
        else:
            lines.append(f"r{i}-{j-1}: {rle}  (x{count})")
        i = j

    # Interactive element index
    if interactive:
        lines.append("")
        lines.append(f"--- Interactive ({len(interactive)}) ---")
        type_counters = {}
        for item in interactive:
            k = item['kind'] or '?'
            type_counters[k] = type_counters.get(k, 0) + 1
            eid = f"{k}{type_counters[k]}"
            label = item['label']
            label_str = f' "{label}"' if label else ''
            lines.append(
                f"{eid}:{label_str} ({item['px']},{item['py']})"
            )

    return '\n'.join(lines)


def render_interactive_only(data, title="", url="", tab=""):
    """Just the interactive element list — no grid, minimal tokens."""
    elements = data['elements']
    interactive = []

    for el in elements:
        if not el.get('i'):
            continue
        ex, ey, ew, eh = el['x'], el['y'], el['w'], el['h']
        interactive.append({
            'kind': el.get('k'),
            'label': el.get('l', ''),
            'px': int(ex + ew / 2), 'py': int(ey + eh / 2),
        })

    interactive.sort(key=lambda e: (e['py'], e['px']))
    interactive = interactive[:MAX_INTERACTIVE]

    lines = []
    lines.append(f"=== Interactive Elements ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Elements: {data['count']} visible, {len(interactive)} interactive")
    lines.append("")

    type_counters = {}
    for item in interactive:
        k = item['kind'] or '?'
        type_counters[k] = type_counters.get(k, 0) + 1
        eid = f"{k}{type_counters[k]}"
        label = item['label']
        label_str = f' "{label}"' if label else ''
        lines.append(f"{eid}:{label_str} ({item['px']},{item['py']})")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# LLM-optimized output formats (minimal tokens, max attention efficiency)
# ---------------------------------------------------------------------------

# Footer junk labels that are never actionable for navigation
_JUNK_LABELS = {
    'terms', 'privacy', 'send product feedback', 'report a problem',
    'united states', 'get app', 'dismiss', 'keyboard shortcuts', 'map data',
    'google apps', 'browse street view images', 'show images',
    'get app', 'show your location',
}

# Patterns that indicate junk (prefix match, lowercased)
_JUNK_PREFIXES = ('google account:', '1 mi', '2 mi', '500 ft', '1000 ft',
                  '200 ft', '1 km', '2 km')


def _strip_pua(s: str) -> str:
    """Remove Private Use Area unicode chars (icon fonts)."""
    return ''.join(c for c in s if ord(c) < 0xE000 or ord(c) > 0xF8FF)


def _is_junk(label: str) -> bool:
    """Filter footer/legal/chrome elements an LLM would never click."""
    low = _strip_pua(label).lower().strip()
    if low in _JUNK_LABELS:
        return True
    for prefix in _JUNK_PREFIXES:
        if low.startswith(prefix):
            return True
    return False


def _canvas_summary(data):
    """Return a compact canvas summary line, or '' if no canvas on page."""
    canvases = [el for el in data['elements'] if el.get('k') == 'C']
    if not canvases:
        return ''
    vw, vh = data['vw'], data['vh']
    parts = []
    for c in canvases[:3]:  # cap at 3
        w, h = c['w'], c['h']
        # Flag if canvas covers most of viewport (map/chart/game)
        full = w >= vw * 0.8 and h >= vh * 0.8
        size = f"{w}x{h}" + (" full" if full else "")
        parts.append(f"{size} @ {c['x']},{c['y']}")
    return "Canvas: " + "; ".join(parts)


def _iframe_summary(data):
    """Return a compact iframe summary line, or '' if no iframes on page."""
    iframes = [el for el in data['elements'] if el.get('k') == 'IF']
    if not iframes:
        return ''
    parts = []
    for f in iframes[:4]:  # cap at 4
        domain = f.get('d', 'cross-origin')
        parts.append(f"{domain} {f['w']}x{f['h']}")
    return "Iframe: " + "; ".join(parts)


def _shadow_summary(data):
    """Return a compact shadow DOM summary line, or '' if none."""
    count = data.get('shadow', 0)
    if not count:
        return ''
    return f"Shadow: {count} hosts"


def _overlay_summary(data):
    """Return a compact overlay/modal summary line, or '' if none detected."""
    ov = data.get('overlay')
    if not ov:
        return ''
    parts = ["[MODAL/OVERLAY DETECTED]"]
    tag_role = f"<{ov['tag']}>"
    if ov.get('role'):
        tag_role += f' role="{ov["role"]}"'
    parts.append(tag_role)
    r = ov.get('rect', {})
    parts.append(f"{r.get('w', 0)}x{r.get('h', 0)} at ({r.get('x', 0)},{r.get('y', 0)})")
    if ov.get('zIndex'):
        parts.append(f"z-index:{ov['zIndex']}")
    if ov.get('buttons'):
        parts.append(f"buttons: {', '.join(ov['buttons'][:3])}")
    if ov.get('fields'):
        parts.append(f"fields: {', '.join(ov['fields'][:3])}")
    return ' | '.join(parts)


def _scroll_summary(data):
    """Return viewport vs page scroll summary, or '' if page fits in viewport."""
    vh = data.get('vh', 0)
    sh = data.get('sh', 0)
    sy = data.get('sy', 0)
    if not sh or sh <= vh:
        return ''
    pct = int(round(sy / sh * 100))
    return f"viewport: {vh}px | page: {sh}px | scroll: {sy}px ({pct}%)"


def _page_hints(data):
    """Return all detected content hints as a single string (or '')."""
    hints = []
    for fn in (_scroll_summary, _overlay_summary, _canvas_summary, _iframe_summary, _shadow_summary):
        line = fn(data)
        if line:
            hints.append(line)
    return '\n'.join(hints)


def _extract_interactive(data, filter_junk=True):
    """Extract sorted interactive elements from DOM walker data."""
    interactive = []
    for el in data['elements']:
        if not el.get('i'):
            continue
        ex, ey, ew, eh = el['x'], el['y'], el['w'], el['h']
        label = el.get('l', '')
        if filter_junk and _is_junk(label):
            continue
        # Skip empty-label or icon-only elements (PUA unicode, no readable text)
        clean = _strip_pua(label).strip()
        if not clean:
            continue
        interactive.append({
            'kind': el.get('k'),
            'label': clean,
            'px': int(ex + ew / 2), 'py': int(ey + eh / 2),
        })
    interactive.sort(key=lambda e: (e['py'], e['px']))
    return interactive[:MAX_INTERACTIVE]


def _label_short(label: str, max_len: int = LABEL_TRUNC_SHORT) -> str:
    """Shorten label for compact formats: keep first N chars, word-boundary cut."""
    label = label.strip()
    if not label:
        return '_'
    if len(label) <= max_len:
        return label
    # Cut at word boundary
    cut = label[:max_len].rsplit(' ', 1)[0]
    return cut if len(cut) > 5 else label[:max_len]


def render_llm_compact(data, title="", url="", tab=""):
    """Format A: One element per line, no decoration.
    Label x,y
    >Label x,y  (> prefix = input field)
    """
    items = _extract_interactive(data)
    lines = []
    if title:
        lines.append(title)
    for item in items:
        prefix = '>' if item['kind'] == 'F' else ''
        lines.append(f"{prefix}{item['label']} {item['px']},{item['py']}")
    return '\n'.join(lines)


def render_llm_line(data, title="", url="", tab=""):
    """Format B: Single line with @ delimiter.
    Label@x,y Label@x,y >Label@x,y
    """
    items = _extract_interactive(data)
    parts = []
    for item in items:
        label = _label_short(item['label'])
        prefix = '>' if item['kind'] == 'F' else ''
        parts.append(f"{prefix}{label}@{item['px']},{item['py']}")
    header = title or 'page'
    return f"{header}\n{' '.join(parts)}"


def render_llm_2pass(data, title="", url="", tab=""):
    """Format C: Labels only for orientation (coords via --resolve on demand).
    PAGE: Label|Label|>Label|Label
    """
    items = _extract_interactive(data)
    lines = []
    if tab:
        lines.append(f"Tab: {tab}")
    parts = []
    for item in items:
        label = _label_short(item['label'])
        prefix = '>' if item['kind'] == 'F' else ''
        parts.append(f"{prefix}{label}@{item['px']},{item['py']}")
    header = title or 'page'
    lines.append(f"{header}: {'|'.join(parts)}")
    return '\n'.join(lines)


def _section_summary(data):
    """Compact Y-band summary: sections(count) per band. ~30 tokens."""
    items = _extract_interactive(data)
    if not items or len(items) <= 5:
        return ""
    bands = []
    current_band = [items[0]]
    for item in items[1:]:
        if item['py'] - current_band[-1]['py'] > Y_BAND_THRESHOLD:
            bands.append(current_band)
            current_band = [item]
        else:
            current_band.append(item)
    bands.append(current_band)
    if len(bands) <= 2:
        return ""  # not enough structure to be useful
    parts = []
    for band in bands:
        y_mid = band[len(band) // 2]['py']
        # Pick best label: first non-empty, prefer short descriptive ones
        label = ""
        for item in band:
            l = item.get('label', '')
            if l and len(l) <= 25:
                label = l
                break
        if not label and band[0].get('label'):
            label = band[0]['label'][:20]
        n = len(band)
        if label:
            parts.append(f"[y{y_mid}]{label}({n})")
        else:
            parts.append(f"[y{y_mid}]({n})")
    return "sections: " + " ".join(parts)


def render_llm_resolve(data, query=""):
    """Resolve a label substring to coordinates. Returns 'label x,y' or 'not found'."""
    items = _extract_interactive(data)
    query_low = query.lower()
    for item in items:
        if query_low in item['label'].lower():
            return f"{item['label']} {item['px']},{item['py']}"
    return "not found"


def render_llm_group(data, title="", url="", tab=""):
    """Format D: Grouped by Y-band (page sections).
    [y] Label@x,y Label@x,y
    """
    items = _extract_interactive(data)
    if not items:
        return f"{title or 'page'}: (empty)"

    # Group by Y-band (elements within Y_BAND_THRESHOLD px = same section)
    bands = []
    current_band = [items[0]]
    for item in items[1:]:
        if item['py'] - current_band[-1]['py'] > Y_BAND_THRESHOLD:
            bands.append(current_band)
            current_band = [item]
        else:
            current_band.append(item)
    bands.append(current_band)

    lines = []
    if title:
        lines.append(title)
    for band in bands:
        y_mid = band[len(band) // 2]['py']
        parts = []
        for item in band:
            label = _label_short(item['label'])
            prefix = '>' if item['kind'] == 'F' else ''
            parts.append(f"{prefix}{label}@{item['px']},{item['py']}")
        lines.append(f"[y{y_mid}] {' '.join(parts)}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Stage 5: Text extraction renderer
# ---------------------------------------------------------------------------

async def _extract_pdf_text(cdp, url=None, max_chars=5000, max_pages=50):
    """Download PDF via browser fetch, extract text with pypdf."""
    import base64
    import io
    from pypdf import PdfReader

    b64 = await cdp.fetch_pdf_base64(url)
    pdf_bytes = base64.b64decode(b64)
    reader = PdfReader(io.BytesIO(pdf_bytes))

    pages = reader.pages[:max_pages]
    text_parts = []
    for page in pages:
        t = page.extract_text() or ""
        if t.strip():
            text_parts.append(t)

    full = "\n\n".join(text_parts)
    return full[:max_chars], len(reader.pages), len(pdf_bytes)


async def _extract_pdf_via_click(cdp, selector, max_chars=5000, max_pages=50):
    """Click a download button, intercept the PDF network response, extract text.

    Uses a JS-side XHR/fetch interceptor to capture the PDF URL, then fetches
    the PDF via the existing browser-fetch pipeline.  This avoids directly
    reading from ``cdp.ws`` (which would steal messages from ``cdp.send``).

    The selector is resolved via CDP DOM.querySelector (no JS string
    interpolation) to prevent injection.  The interceptors save and restore
    the original XHR.open and fetch on cleanup.

    Returns ``(text, n_pages, size_bytes, pdf_url)`` — a 4-tuple (contrast
    with ``_extract_pdf_text`` which returns a 3-tuple without the URL).
    """
    import base64
    import io
    from pypdf import PdfReader

    # Install XHR + fetch interceptor (captures ALL URLs — filtering happens
    # later on the Python side so we don't miss non-obvious PDF endpoints).
    # Originals are saved on the window object for restoration.
    setup_js = '''
    (() => {
        window.__uc_pdf_urls = [];
        window.__uc_orig_xhr_open = XMLHttpRequest.prototype.open;
        window.__uc_orig_fetch = window.fetch;
        XMLHttpRequest.prototype.open = function(method, url) {
            if (url) window.__uc_pdf_urls.push(url);
            return window.__uc_orig_xhr_open.apply(this, arguments);
        };
        // Use .call(window, ...) instead of .apply(this, ...) because fetch
        // is called as a standalone function — `this` would be undefined in
        // strict mode.
        window.fetch = function() {
            var input = arguments[0];
            var url = typeof input === "string" ? input : (input && input.url) || "";
            if (url) window.__uc_pdf_urls.push(url);
            return window.__uc_orig_fetch.apply(window, arguments);
        };
        return "INTERCEPTORS_INSTALLED";
    })()
    '''
    await cdp.execute_js(setup_js)

    try:
        # Resolve the selector via CDP DOM API (injection-safe — selector is a
        # protocol parameter, never interpolated into JS).
        await cdp.send("DOM.enable")
        doc = await cdp.send("DOM.getDocument")
        root_id = doc["root"]["nodeId"]
        qresult = await cdp.send("DOM.querySelector", {
            "nodeId": root_id, "selector": selector
        })
        node_id = qresult.get("nodeId")
        if not node_id or node_id == 0:
            raise RuntimeError(f"Selector '{selector}' not found on page")

        # Resolve the DOM node to a JS object and click via callFunctionOn
        resolved = await cdp.send("DOM.resolveNode", {"nodeId": node_id})
        object_id = resolved.get("object", {}).get("objectId")
        if not object_id:
            raise RuntimeError(f"Could not resolve DOM node for '{selector}'")
        await cdp.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": "function() { this.click(); }",
        })

        # Poll for captured PDF URL (up to 15s)
        pdf_url = None
        for _ in range(30):  # 30 x 0.5s = 15s
            await asyncio.sleep(0.5)
            result = await cdp.execute_js(
                "JSON.stringify(window.__uc_pdf_urls || [])")
            urls_raw = result.get("result", {}).get("value", "[]")
            urls = json.loads(urls_raw)
            if urls:
                # Pick the most likely PDF URL: prefer urls containing 'pdf' or
                # 'download', fall back to the last captured URL (most recent).
                pdf_url = next(
                    (u for u in urls if "pdf" in u.lower() or "download" in u.lower()),
                    urls[-1])
                break
    finally:
        await _cleanup_pdf_interceptors(cdp)

    if not pdf_url:
        raise RuntimeError("No PDF download URL captured within 15s after clicking")

    # Resolve relative URLs against the page origin
    if pdf_url.startswith("/"):
        origin_result = await cdp.execute_js("window.location.origin")
        origin = origin_result.get("result", {}).get("value", "")
        if origin:
            pdf_url = origin + pdf_url

    # SSRF guard: only allow http/https schemes (page JS could inject
    # file:// or internal-network URLs into __uc_pdf_urls).
    if not pdf_url.startswith(("https://", "http://")):
        raise RuntimeError(f"Rejected PDF URL with disallowed scheme: {pdf_url!r}")

    # Fetch and extract using the existing pipeline
    b64 = await cdp.fetch_pdf_base64(pdf_url)
    pdf_bytes = base64.b64decode(b64)

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = reader.pages[:max_pages]
    text_parts = []
    for page in pages:
        t = page.extract_text() or ""
        if t.strip():
            text_parts.append(t)

    full = "\n\n".join(text_parts)
    return full[:max_chars], len(reader.pages), len(pdf_bytes), pdf_url


async def _cleanup_pdf_interceptors(cdp):
    """Restore original XHR.open and fetch, remove capture state, disable DOM."""
    await cdp.execute_js('''
        (() => {
            if (window.__uc_orig_xhr_open)
                XMLHttpRequest.prototype.open = window.__uc_orig_xhr_open;
            if (window.__uc_orig_fetch)
                window.fetch = window.__uc_orig_fetch;
            delete window.__uc_pdf_urls;
            delete window.__uc_orig_xhr_open;
            delete window.__uc_orig_fetch;
        })()
    ''')
    try:
        await cdp.send("DOM.disable")
    except Exception:
        pass  # best-effort; DOM.disable failure is non-critical


def render_pdf_text(text, n_pages, size_bytes, title="", url="", tab="", find=""):
    """Format extracted PDF text with header."""
    lines = []
    lines.append("=== PDF Text ===")
    lines.append("Extractor: pypdf | Fetch: browser-session (cookies inherited)")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Pages: {n_pages} | Size: {size_bytes // 1024}KB | Extracted: {len(text)} chars")

    if find:
        idx = text.lower().find(find.lower())
        if idx == -1:
            lines.append(f'Find: "{find}" not found')
            lines.append("")
            lines.append(text[:500])
        else:
            start = max(0, idx - 200)
            end = min(len(text), idx + 300)
            lines.append(f'Find: "{find}" at char {idx}')
            lines.append("")
            if start > 0:
                lines.append("...")
            lines.append(text[start:end])
            if end < len(text):
                lines.append("...")
    else:
        lines.append("")
        lines.append(text)

    return '\n'.join(lines)


def render_text(text, title="", url="", tab="", find=""):
    """Format extracted page text with header. Pure function, no CDP."""
    lines = []
    lines.append("=== Page Text ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Length: {len(text)} chars")

    if find:
        # Show context around keyword match
        idx = text.lower().find(find.lower())
        if idx == -1:
            lines.append(f'Find: "{find}" not found')
            lines.append("")
            lines.append(text[:500])
        else:
            start = max(0, idx - 200)
            end = min(len(text), idx + 300)
            lines.append(f'Find: "{find}" at char {idx}')
            lines.append("")
            if start > 0:
                lines.append("...")
            lines.append(text[start:end])
            if end < len(text):
                lines.append("...")
    else:
        lines.append("")
        lines.append(text)

    return '\n'.join(lines)


def _find_nearby_interactives(interactives, target_y, max_dist=FIND_NEARBY_PX):
    """Filter interactive elements within ±max_dist pixels vertically of target_y.

    Returns up to 3 elements sorted by vertical distance from target_y.
    Pure function, no CDP.
    """
    nearby = []
    for el in interactives:
        dist = abs(el['py'] - target_y)
        if dist <= max_dist:
            nearby.append((dist, el))
    nearby.sort(key=lambda t: t[0])
    return [el for _, el in nearby[:3]]


def render_find(find_data, dom_data, title="", url="", tab="", keyword=""):
    """Format DOM-aware find results with nearby interactive elements.

    find_data: list of dicts from TEXT_FIND_JS (px, py, tag, ctx, below)
    dom_data: DOM walker data dict (or None for graceful fallback)
    Pure function, no CDP.
    """
    lines = []
    lines.append(f'=== Find: "{keyword}" ===')
    if tab:
        lines.append(f"Tab: {tab}")
    if title:
        lines.append(f"Page: {title}")
    lines.append(f"Matches: {len(find_data)}")

    interactives = _extract_interactive(dom_data) if dom_data else []

    for i, match in enumerate(find_data, 1):
        px, py = match['px'], match['py']
        tag = match.get('tag', '?')
        ctx = match.get('ctx', '')
        below = match.get('below', False)

        below_marker = "  [below fold]" if below else ""
        lines.append("")
        lines.append(f"[{i}] ({px},{py}) <{tag}>{below_marker}")
        lines.append(f"  {ctx}")

        nearby = _find_nearby_interactives(interactives, py)
        if nearby:
            parts = []
            for el in nearby:
                kind = el.get('kind', '?')
                label = _label_short(el['label'])
                parts.append(f'{kind} "{label}" ({el["px"]},{el["py"]})')
            lines.append(f"  -> {' | '.join(parts)}")

    return '\n'.join(lines)


def render_api_scan(data, tab=""):
    """Format API scan results (~200-500 tokens)."""
    lines = []
    mode = data.get('mode', 'scan')
    results = data.get('results', [])
    ms = data.get('ms', 0)

    if mode == 'scan':
        lines.append("=== API Scan ===")
    elif mode == 'method':
        lines.append(f"=== API Method Search: {data.get('search', '')} ===")
    elif mode == 'dotpath':
        lines.append(f"=== API Dot-Path Search: {data.get('search', '')} ===")

    if tab:
        lines.append(f"Tab: {tab}")
    lines.append(f"Found: {len(results)} API instance{'s' if len(results) != 1 else ''} ({ms}ms)")
    lines.append("")

    if not results:
        lines.append("No API instances found.")
        return '\n'.join(lines)

    for r in results:
        path = r.get('path', '?')
        rtype = r.get('type', '?')
        methods = r.get('methods', 0)
        sample = r.get('sample', [])
        sig = r.get('sig')

        header = f"  {path}"
        if sig:
            header += f"  [{sig['name']} {sig['matched']}/{sig['total']}]"
        lines.append(header)
        lines.append(f"    type: {rtype}, methods: {methods}")
        if sample:
            lines.append(f"    sample: {', '.join(sample)}")
        lines.append("")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Main: CDP connect, optional navigate, execute JS, render
# ---------------------------------------------------------------------------

async def run(args):
    url = None
    max_cols = DEFAULT_COLS
    blocks = False
    sparse = False
    interactive_only = False
    forms_mode = False
    json_mode = False
    llm_mode = None  # None, 'compact', 'line', '2pass', 'group'
    resolve_query = None  # --resolve <label>
    at_coord = None  # (px, py) or ('g', col, row)
    tab_id = None     # --tab <id>
    list_tabs = False  # --tabs
    new_tab = False    # --new
    close_tab_id = None  # --close <id>
    text_mode = False    # --text
    text_find = None     # --find <keyword>
    text_max = DEFAULT_TEXT_MAX  # --max <chars>
    js_expr = None       # --js <expression>
    api_mode = False     # --api
    api_search = None    # --api <search_term>
    pdf_mode = False     # --pdf
    pdf_url = None       # --pdf [url]
    pdf_click_selector = None  # --click <selector> (use with --pdf)
    type_text = None     # --type <text>
    no_probe = False     # --no-probe (skip intel probe in batch)

    # --help / -h
    if '--help' in args or '-h' in args:
        print("""Usage: ddm [options] [url]

Options:
  --port <int>     Chrome debug port (default: 9222)
  --tab <id>       Use specific tab (prefix match OK)
  --tabs           List open tabs and exit
  --new            Open new tab
  --close <id>     Close tab
  --llm-2pass      LLM-optimized 2-pass output (recommended)
  --llm            LLM compact output
  --llm-line       LLM line-by-line output
  --llm-group      LLM grouped output
  --sparse         RLE-compressed sparse output
  --interactive    Interactive elements only
  --json           Structured JSON output
  --blocks         Unicode block art mode
  --text           Extract page innerText
  --find <kw>      Find keyword in text (use with --text)
  --max <chars>    Limit text output (use with --text)
  --resolve <lbl>  Reverse lookup: label -> coordinates
  --at <coords>    Reverse lookup: coords -> elements (px: 694,584 or grid: g48,40)
  --forms          Detect forms as tool contracts
  --type <text>     Type text via real CDP key events (SPA-compatible)
  --js <expr>      Execute JavaScript on the page and print result
  --pdf            Extract text from PDF (auto-detect or current page)
  --pdf <url>      Download PDF from URL and extract text
  --api            Scan window for app API instances (objects with methods)
  --api <name>     Find which global has this method (e.g. "insertVertex")
  --api <dot.path> Find by dot-path shape (e.g. "editor.graph")
  --cols <int>     Grid width (default 160)
  -h, --help       Show this help""")
        return

    # Parse args
    i = 0
    while i < len(args):
        if args[i] == '--cols' and i + 1 < len(args):
            max_cols = int(args[i + 1])
            i += 2
        elif args[i] == '--blocks':
            blocks = True
            i += 1
        elif args[i] == '--sparse':
            sparse = True
            i += 1
        elif args[i] == '--interactive':
            interactive_only = True
            i += 1
        elif args[i] == '--forms':
            forms_mode = True
            i += 1
        elif args[i] == '--json':
            json_mode = True
            i += 1
        elif args[i] == '--no-probe':
            no_probe = True
            i += 1
        elif args[i] == '--llm':
            llm_mode = 'compact'
            i += 1
        elif args[i] == '--llm-line':
            llm_mode = 'line'
            i += 1
        elif args[i] == '--llm-2pass':
            llm_mode = '2pass'
            i += 1
        elif args[i] == '--llm-auto':
            llm_mode = 'auto'
            i += 1
        elif args[i] == '--llm-group':
            llm_mode = 'group'
            i += 1
        elif args[i] == '--resolve' and i + 1 < len(args):
            resolve_query = args[i + 1]
            i += 2
        elif args[i] == '--at' and i + 1 < len(args):
            raw = args[i + 1]
            if raw.startswith('g'):
                # Grid coords: g48,40
                parts = raw[1:].split(',')
                at_coord = ('g', int(parts[0]), int(parts[1]))
            else:
                # Pixel coords: 694,584
                parts = raw.split(',')
                at_coord = ('px', int(parts[0]), int(parts[1]))
            i += 2
        elif args[i] == '--port' and i + 1 < len(args):
            global _CDP_PORT
            _CDP_PORT = int(args[i + 1])
            i += 2
        elif args[i] == '--tab' and i + 1 < len(args):
            val = args[i + 1]
            tab_id = None if val == "auto" else val
            i += 2
        elif args[i] == '--tabs':
            list_tabs = True
            i += 1
        elif args[i] == '--new':
            new_tab = True
            i += 1
        elif args[i] == '--close' and i + 1 < len(args):
            close_tab_id = args[i + 1]
            i += 2
        elif args[i] == '--text':
            text_mode = True
            i += 1
        elif args[i] == '--find' and i + 1 < len(args):
            text_find = args[i + 1]
            i += 2
        elif args[i] == '--max' and i + 1 < len(args):
            text_max = int(args[i + 1])
            i += 2
        elif args[i] == '--js' and i + 1 < len(args):
            js_expr = args[i + 1]
            i += 2
        elif args[i] == '--type' and i + 1 < len(args):
            type_text = args[i + 1]
            i += 2
        elif args[i] == '--pdf':
            pdf_mode = True
            if i + 1 < len(args) and not args[i + 1].startswith('-'):
                pdf_url = args[i + 1]
                i += 2
            else:
                pdf_url = None
                i += 1
        elif args[i] == '--click' and i + 1 < len(args):
            pdf_click_selector = args[i + 1]
            i += 2
            continue
        elif args[i] == '--api':
            api_mode = True
            if i + 1 < len(args) and not args[i + 1].startswith('-'):
                api_search = args[i + 1]
                i += 2
            else:
                api_search = None
                i += 1
        elif not args[i].startswith('-'):
            url = args[i]
            i += 1
        else:
            print(f"ERROR: Unknown flag '{args[i]}'. Run with --help for usage.", file=sys.stderr)
            sys.exit(1)

    _ensure_chrome()

    # --tabs: list available tabs and exit
    if list_tabs:
        tabs = _list_tabs()
        print(f"=== CDP Tabs ({len(tabs)}) ===")
        for t in tabs:
            tid = t["id"]
            title = t.get("title", "")[:50]
            turl = t.get("url", "")[:80]
            popup = "  [popup]" if t.get("type") == "popup" else ""
            print(f"  {tid}  {title or '(empty)'}{popup}  {turl}")
        return

    # --close: close a tab and exit
    if close_tab_id:
        if _close_tab(close_tab_id):
            print(f"Closed tab {close_tab_id}")
        else:
            print(f"Failed to close tab {close_tab_id}", file=sys.stderr)
        return

    # Resolve which tab to connect to
    if new_tab:
        tab_info = _create_new_tab(url or "about:blank")
        ws_url = tab_info["webSocketDebuggerUrl"]
        tab_id = tab_info["id"]
        print(f"tab:{tab_id}", file=sys.stderr)
    elif tab_id:
        ws_url = _get_ws_url_for_tab(tab_id)
    else:
        tabs = _list_tabs()
        if not tabs:
            raise RuntimeError("No page tab found")
        pages_only = [t for t in tabs if t.get("type") == "page"]
        default_tab = pages_only[0] if pages_only else tabs[0]
        tab_id = default_tab["id"]
        ws_url = default_tab["webSocketDebuggerUrl"]

    cdp = CDP(ws_url)
    await cdp.connect()

    try:
        if url:
            await cdp.navigate(url, wait=3)

        # --api: scan window for app API instances
        if api_mode:
            search_arg = 'null' if api_search is None else json.dumps(api_search)
            js = API_SCAN_JS % search_arg
            result = await cdp.execute_js(js)
            res = result.get("result", {})
            if "exceptionDetails" in result:
                desc = result["exceptionDetails"].get("exception", {}).get("description", "Unknown error")
                print(f"API scan error: {desc}", file=sys.stderr)
                sys.exit(1)
            value = res.get("value")
            if not value:
                print("=== API Scan ===")
                print("No results (page may not have loaded yet)")
                return
            print(render_api_scan(value, tab=tab_id))
            return

        # --type: type text via real CDP key events (SPA-compatible)
        if type_text is not None:
            import asyncio as _aio
            # Auto-focus: use focused input/textarea, or find single visible one
            auto_focus_js = (
                "(()=>{"
                "const el=document.activeElement;"
                "if(el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA')){"
                "const lbl=el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.name||'';"
                "return 'focused: '+el.tagName+(lbl?' \"'+lbl+'\"':'')}"
                "const inputs=[...document.querySelectorAll('input:not([type=hidden]),textarea')]"
                ".filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0});"
                "if(inputs.length===1){inputs[0].focus();"
                "const lbl=inputs[0].getAttribute('aria-label')||inputs[0].getAttribute('placeholder')||inputs[0].name||'';"
                "return 'auto-focused: '+inputs[0].tagName+(lbl?' \"'+lbl+'\"':'')}"
                "return 'no input focused ('+inputs.length+' visible inputs — click one first)'"
                "})()"
            )
            focus_res = await cdp.execute_js(auto_focus_js)
            focus_info = focus_res.get("result", {}).get("value", "")
            if focus_info.startswith("no input"):
                print(focus_info)
                return
            # Snapshot before
            _sel = (
                "button,a,input,textarea,select,"
                "[role=button],[role=option],[role=menuitem],[role=tab],[role=link],[role=listbox]"
            )
            before_js = (
                f"(()=>{{const s='{_sel}';"
                "const els=new Set([...document.querySelectorAll(s)]"
                ".filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0}));"
                "window._ub={els,url:location.href,title:document.title,focus:document.activeElement};"
                "return 'ok'"
                "})()"
            )
            await cdp.execute_js(before_js)
            # Send real CDP key events per character
            for char in type_text:
                await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": char})
                await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "text": char})
                await _aio.sleep(0.05)
            await _aio.sleep(0.3)
            # Diff after
            after_js = (
                "(()=>{"
                "const b=window._ub;if(!b)return '';"
                f"const s='{_sel}';"
                "const aEls=[...document.querySelectorAll(s)]"
                ".filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0});"
                "const nw=aEls.filter(e=>!b.els.has(e));"
                "const f=document.activeElement;"
                "let d=[];"
                "if(location.href!==b.url)d.push('url: '+location.href);"
                "if(document.title!==b.title)d.push('title: '+document.title);"
                "if(f!==b.focus){"
                "const ft=f.tagName;"
                "const fl=f.getAttribute('aria-label')||f.getAttribute('placeholder')||f.name||'';"
                "const fv=(ft==='INPUT'||ft==='TEXTAREA')?(f.value||'').substring(0,60):'';"
                "d.push('focus→ '+ft+(fl?' \"'+fl+'\"':'')+(fv?' val=\"'+fv+'\"':''))}"
                "if(nw.length>0&&nw.length<=15){"
                "nw.slice(0,8).forEach(e=>{"
                "const r=e.getBoundingClientRect();"
                "const cx=Math.round(r.x+r.width/2);"
                "const cy=Math.round(r.y+r.height/2);"
                "const lbl=(e.getAttribute('aria-label')||e.textContent||'').trim().substring(0,40);"
                "const role=e.getAttribute('role');"
                "d.push('+ '+(role||e.tagName)+' \"'+lbl+'\" at ('+cx+','+cy+')')});"
                "if(nw.length>8)d.push('+ ...'+(_new.length-8)+' more')"
                "}else if(nw.length>15){"
                "d.push('page updated: '+aEls.length+' interactive elements')}"
                "if(d.length){"
                "return '\\n--- changed ---\\n'+d.join('\\n')}"
                "const f2=document.activeElement;"
                "const fl2=f2.getAttribute('aria-label')||f2.getAttribute('placeholder')||f2.name||'';"
                "const fv2=(f2.tagName==='INPUT'||f2.tagName==='TEXTAREA')?(f2.value||'').substring(0,60):'';"
                "return '\\n--- no change --- (focus: '+f2.tagName"
                "+(fl2?' \"'+fl2+'\"':'')+(fv2?' val=\"'+fv2+'\"':'')"
                "+' | '+location.hostname+')'"
                "})()"
            )
            diff_res = await cdp.execute_js(after_js)
            diff = diff_res.get("result", {}).get("value", "")
            print(f"typed into {focus_info}{diff}")
            return

        # --js: execute JavaScript and print result, then exit
        if js_expr:
            result = await cdp.execute_js(js_expr)
            res = result.get("result", {})
            if "exceptionDetails" in result:
                desc = result["exceptionDetails"].get("exception", {}).get("description", "Unknown error")
                print(f"JS Error: {desc}", file=sys.stderr)
                sys.exit(1)
            value = res.get("value")
            if value is None and res.get("type") == "undefined":
                print("undefined")
            elif isinstance(value, (dict, list)):
                import json as _json
                print(_json.dumps(value, indent=2))
            else:
                print(value)
            return

        # --pdf: extract text from PDF
        if pdf_mode:
            if pdf_click_selector:
                # Click-to-extract: intercept PDF download triggered by click
                try:
                    text, n_pages, size_bytes, captured_url = \
                        await _extract_pdf_via_click(
                            cdp, pdf_click_selector, max_chars=text_max)
                    page_title, _ = await cdp.get_page_info()
                    print(render_pdf_text(text, n_pages, size_bytes,
                                          title=page_title,
                                          url=captured_url,
                                          tab=tab_id, find=text_find))
                except Exception as e:
                    print(f"PDF click-extract error: {e}", file=sys.stderr)
                    sys.exit(1)
                return
            if pdf_url:
                # Navigate to the URL first so title/URL are available
                await cdp.navigate(pdf_url, wait=3)
            page_title, page_url = await cdp.get_page_info()
            try:
                text, n_pages, size_bytes = await _extract_pdf_text(
                    cdp, url=pdf_url, max_chars=text_max)
                print(render_pdf_text(text, n_pages, size_bytes,
                                      title=page_title, url=page_url,
                                      tab=tab_id, find=text_find))
            except Exception as e:
                print(f"PDF extraction error: {e}", file=sys.stderr)
                sys.exit(1)
            return

        # Batch: page info + DOM walker (+ optional intel probe) in 1 round-trip
        _probe_fingerprint = {}
        _probe_stores = []
        _batch_exprs = [
            "JSON.stringify({t:document.title,u:window.location.href})",
            "typeof __uc_domWalk==='function'?__uc_domWalk():null",
        ]
        if not no_probe:
            try:
                from unchained_cli.intel_engine import (FINGERPRINT_JS, STORE_PROBE_JS,
                                                        categorize_features, bayesian_rank,
                                                        render_fingerprint, render_strategy_line)
            except ImportError:
                from intel_engine import (FINGERPRINT_JS, STORE_PROBE_JS,
                                          categorize_features, bayesian_rank,
                                          render_fingerprint, render_strategy_line)
            _batch_exprs.append(FINGERPRINT_JS)
            _batch_exprs.append(STORE_PROBE_JS)
        _batch = await cdp.batch_evaluate(_batch_exprs)
        _info = json.loads(_batch[0].get("result", {}).get("value", "{}"))
        page_title = _info.get("t", "")
        page_url = _info.get("u", "")
        _dom_batch_data = _batch[1].get("result", {}).get("value")
        if not no_probe:
            _probe_fingerprint = _batch[2].get("result", {}).get("value", {})
            _probe_stores = _batch[3].get("result", {}).get("value", [])

        if at_coord:
            # Reverse lookup mode
            if at_coord[0] == 'g':
                # Convert grid coords to pixel coords using current viewport
                vw_result = await cdp.execute_js("window.innerWidth")
                vw = vw_result.get("result", {}).get("value", 1920)
                cell_px = vw / max_cols
                px = int(at_coord[1] * cell_px + cell_px / 2)
                py = int(at_coord[2] * cell_px + cell_px / 2)
            else:
                px, py = at_coord[1], at_coord[2]

            js = ELEMENTS_AT_JS % (px, py)
            result = await cdp.execute_js(js)
            elements = result.get("result", {}).get("value")
            if not elements:
                print(f"No elements found at px({px},{py})")
                return
            output = render_elements_at(elements, px, py, tab=tab_id)
            print(output)
            return

        # Forms mode — separate JS, no DOM walker needed
        if forms_mode:
            result = await cdp.execute_js(FORMS_JS)
            forms_data = result.get("result", {}).get("value", [])
            if json_mode:
                print(json.dumps({'page': page_title, 'url': page_url,
                                  'tab': tab_id, 'forms': forms_data}, indent=2))
            else:
                print(render_forms(forms_data, title=page_title, url=page_url, tab=tab_id))
            return

        # Text mode — extract innerText, no DOM walker needed
        # Auto-detect PDF pages and use PDF extraction instead
        if text_mode:
            if await cdp.is_pdf():
                try:
                    text, n_pages, size_bytes = await _extract_pdf_text(
                        cdp, max_chars=text_max)
                    print(render_pdf_text(text, n_pages, size_bytes,
                                          title=page_title, url=page_url,
                                          tab=tab_id, find=text_find))
                except Exception as e:
                    print(f"PDF extraction error: {e}", file=sys.stderr)
                    sys.exit(1)
            elif text_find:
                # DOM-aware find: TreeWalker search + DOM walker in 1 round-trip
                kw_escaped = json.dumps(text_find)
                find_js = TEXT_FIND_JS % (kw_escaped, 10)
                _find_batch = await cdp.batch_evaluate([
                    find_js,
                    "typeof __uc_domWalk==='function'?__uc_domWalk():null",
                ])
                find_matches = _find_batch[0].get("result", {}).get("value", [])
                dom_data = _find_batch[1].get("result", {}).get("value")
                if find_matches:
                    print(render_find(find_matches, dom_data,
                                      title=page_title, url=page_url,
                                      tab=tab_id, keyword=text_find))
                else:
                    # No DOM matches — fall back to flat text search
                    text = await cdp.get_text(max_len=text_max)
                    print(render_text(text, title=page_title, url=page_url,
                                      tab=tab_id, find=text_find))
            else:
                text = await cdp.get_text(max_len=text_max)
                print(render_text(text, title=page_title, url=page_url,
                                  tab=tab_id, find=text_find))
            return

        # DOM walker result from batch (or install + call if first visit)
        data = _dom_batch_data
        if data is None:
            # Not installed — install + call in one shot (~8KB, once per page)
            fn_body = DOM_WALKER_JS.strip()
            install_js = ("window.__uc_domWalk=" + fn_body[1:fn_body.rfind(')(')]
                          + ";__uc_domWalk()")
            result = await cdp.execute_js(install_js)
            data = result.get("result", {}).get("value")

        if not data:
            print("Error: DOM walker returned no data")
            return

        if json_mode:
            # Structured JSON output — elements + interactive list
            elements = data['elements']
            interactive = [
                {'kind': el.get('k'), 'label': el.get('l', ''),
                 'name': el.get('n', ''), 'inputType': el.get('it', ''),
                 'px': int(el['x'] + el['w'] / 2),
                 'py': int(el['y'] + el['h'] / 2)}
                for el in elements if el.get('i')
            ]
            interactive.sort(key=lambda e: (e['py'], e['px']))
            out = {
                'page': page_title, 'url': page_url, 'tab': tab_id,
                'viewport': {'w': data['vw'], 'h': data['vh']},
                'scroll': {'y': data.get('sy', 0), 'pageHeight': data.get('sh', 0)},
                'totalElements': data['count'],
                'interactive': interactive[:MAX_INTERACTIVE],
            }
            if data.get('overlay'):
                out['overlay'] = data['overlay']
            print(json.dumps(out, indent=2))
            return

        if resolve_query:
            output = render_llm_resolve(data, query=resolve_query)
            print(output)
            return

        if llm_mode:
            # Auto-select optimal mode based on page characteristics
            if llm_mode == 'auto':
                n_els = len(_extract_interactive(data))
                has_stores = bool(_probe_stores)
                layout = data.get('layout', '')
                has_sidebar = 'sidebar' in layout
                has_cards = 'cards' in layout
                if n_els <= 25:
                    llm_mode = 'group'    # few elements → Y-bands tell the story
                elif has_stores or n_els >= 50:
                    llm_mode = 'sparse'   # complex SPA → need B/L/F markers
                elif has_sidebar or has_cards:
                    llm_mode = 'group'    # structured layout → Y-bands
                else:
                    llm_mode = 'line'     # flat list → clean labels

            if llm_mode == 'sparse':
                output = render_sparse_map(data, title=page_title, url=page_url,
                                           max_cols=max_cols, tab=tab_id)
            else:
                renderers = {
                    'compact': render_llm_compact,
                    'line': render_llm_line,
                    '2pass': render_llm_2pass,
                    'group': render_llm_group,
                }
                output = renderers[llm_mode](data, title=page_title, url=page_url, tab=tab_id)
            hints = _page_hints(data)
            n_interactive = len(_extract_interactive(data))
            layout_line = data.get('layout', '')
            sections = _section_summary(data) if llm_mode != 'group' else ""
            meta = f"--- ddm | mode: {llm_mode} | elements: {n_interactive} ---"
            if sections:
                meta += f"\n{sections}"
            if layout_line:
                meta += f"\nlayout: {layout_line}"
            # Append intel probe summary (free — already in batch)
            probe_line = ""
            if _probe_fingerprint:
                try:
                    observations = categorize_features(_probe_fingerprint, _probe_stores)
                    ranked = bayesian_rank(observations)
                    probe_line = "\n" + render_fingerprint(_probe_fingerprint, _probe_stores)
                    probe_line += "\n" + render_strategy_line(ranked)
                except Exception:
                    pass
            print(output + (f"\n{hints}" if hints else "") + f"\n{meta}" + probe_line)
            return

        if interactive_only:
            output = render_interactive_only(data, title=page_title, url=page_url, tab=tab_id)
        elif sparse:
            output = render_sparse_map(data, title=page_title, url=page_url,
                                       max_cols=max_cols, blocks=blocks, tab=tab_id)
        else:
            output = render_density_map(data, title=page_title, url=page_url,
                                        max_cols=max_cols, blocks=blocks, tab=tab_id)
        print(output)

        # Check for page content hints (canvas, iframes, shadow DOM)
        hints = _page_hints(data)
        if hints:
            print(f"\n{hints}")

        # Check for WebMCP tools (sparse/interactive only — adds ~1 line)
        if sparse or interactive_only:
            wmcp = await cdp.execute_js(r"""
                (function() {
                    var mc = navigator.modelContext;
                    if (!mc) return '';
                    var t = window.__wmcp ? window.__wmcp.tools : {};
                    var names = Object.keys(t);
                    return names.length ? names.join(', ') : 'API present, no tools';
                })()
            """)
            wmcp_val = wmcp.get('result', {}).get('value', '')
            if wmcp_val:
                print(f"\nWebMCP: {wmcp_val}")

    finally:
        if cdp.ws:
            await cdp.ws.close()


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
