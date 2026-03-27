"""Bayesian Page Intelligence — automatic extraction strategy for CDP browsing.

Probes the DOM for structural features (shadow roots, custom elements, JS globals,
framework detection), applies Naive Bayes to rank 8 extraction strategies, executes
the best one, and returns compressed output (~100-500 tokens).

Usage:
    intel --probe                          # Fingerprint + strategy ranking
    intel --extract                        # Full pipeline: probe → extract
    intel --extract --strategy host_attrs  # Force a strategy
    intel --shape ytInitialData            # Shape of a JS global
    intel --find-paths ytInitialData title # Find key paths in global
    intel --stores                         # List all JS data stores
    intel --tab <id> --probe              # Probe a specific tab
"""

import asyncio
import json
import math
import os
import sys
import urllib.parse
import urllib.request

import websockets

# ---------------------------------------------------------------------------
# Standalone CDP shim (same as ddm_engine.py)
# ---------------------------------------------------------------------------
_CDP_HOST = "127.0.0.1"
_CDP_PORT = 9222


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

    async def execute_js(self, expression: str) -> dict:
        return await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })

    async def navigate(self, url: str, wait: float = 5):
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
            if resp.get("id") == nav_id:
                nav_done = True
                if load_fired:
                    break
                continue
            if resp.get("method", "") in ("Page.loadEventFired", "Page.frameStoppedLoading"):
                load_fired = True
                if nav_done:
                    break

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

    async def get_page_info(self) -> tuple:
        result = await self.execute_js(
            "JSON.stringify({t:document.title,u:window.location.href})")
        raw = result.get("result", {}).get("value", "{}")
        info = json.loads(raw)
        return info.get("t", ""), info.get("u", "")


def _list_tabs() -> list[dict]:
    req = urllib.request.Request(f"http://{_CDP_HOST}:{_CDP_PORT}/json")
    with urllib.request.urlopen(req, timeout=3) as resp:
        tabs = json.loads(resp.read())
    return [t for t in tabs if t.get("type") in ("page", "popup")]


def _get_ws_url_for_tab(tab_id: str) -> str:
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
    encoded = urllib.parse.quote(url, safe=':/?#[]@!$&\'()*+,;=-._~')
    req = urllib.request.Request(
        f"http://{_CDP_HOST}:{_CDP_PORT}/json/new?{encoded}", method="PUT"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _close_tab(tab_id: str) -> bool:
    req = urllib.request.Request(
        f"http://{_CDP_HOST}:{_CDP_PORT}/json/close/{tab_id}", method="PUT"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().strip() == b"Target is closing"


def _ensure_chrome():
    try:
        req = urllib.request.Request(f"http://{_CDP_HOST}:{_CDP_PORT}/json/version")
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
    except Exception:
        print(f"Chrome not reachable at {_CDP_HOST}:{_CDP_PORT}.", file=sys.stderr)
        print(f"Launch with: unchained launch --port {_CDP_PORT}", file=sys.stderr)
        sys.exit(1)


def _get_ws_url(tab_id: str | None = None) -> str:
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
# Stage 1: Fingerprint JS — counts DOM structural features (~20 tokens output)
# ---------------------------------------------------------------------------

FINGERPRINT_JS = r"""
(function() {
    var all = document.querySelectorAll('*');
    var totalEls = all.length;
    var customEls = 0, shadowRoots = 0;
    var richAttrEls = 0, totalRichAttrs = 0;
    var dataTestIds = 0;
    var iframes = document.querySelectorAll('iframe').length;
    var canvases = document.querySelectorAll('canvas').length;
    var imgs = document.querySelectorAll('img').length;

    // Framework detection — check common root IDs + first body children
    var reactFiber = false;
    var candidates = [
        document.getElementById('root'),
        document.getElementById('__next'),
        document.getElementById('app'),
        document.getElementById('__app'),
        document.getElementById('main'),
    ];
    // Also check first 5 children of body
    if (document.body) {
        for (var ci = 0; ci < Math.min(document.body.children.length, 5); ci++) {
            candidates.push(document.body.children[ci]);
        }
    }
    for (var ci = 0; ci < candidates.length && !reactFiber; ci++) {
        var rootEl = candidates[ci];
        if (!rootEl) continue;
        var keys = Object.keys(rootEl);
        for (var ki = 0; ki < keys.length; ki++) {
            if (keys[ki].indexOf('__reactFiber') === 0 || keys[ki].indexOf('__reactInternalInstance') === 0) {
                reactFiber = true;
                break;
            }
        }
    }

    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var tag = el.tagName.toLowerCase();

        // Custom element: contains hyphen (web component spec)
        if (tag.indexOf('-') !== -1) customEls++;

        // Shadow DOM
        if (el.shadowRoot) shadowRoots++;

        // Rich attributes (>8 attrs suggests data-bearing host element)
        var nAttrs = el.attributes.length;
        if (nAttrs > 8) {
            richAttrEls++;
            totalRichAttrs += nAttrs;
        }

        // data-testid
        if (el.hasAttribute('data-testid')) dataTestIds++;
    }

    var avgAttrs = richAttrEls > 0 ? Math.round(totalRichAttrs / richAttrEls) : 0;

    return {
        totalEls: totalEls,
        customEls: customEls,
        shadowRoots: shadowRoots,
        richAttrEls: richAttrEls,
        avgAttrs: avgAttrs,
        dataTestIds: dataTestIds,
        iframes: iframes,
        canvases: canvases,
        imgs: imgs,
        reactFiber: reactFiber
    };
})()
"""

# ---------------------------------------------------------------------------
# Stage 2: Store Probe JS — checks for known JS globals (~15 tokens output)
# ---------------------------------------------------------------------------

STORE_PROBE_JS = r"""
(function() {
    var known = [
        'ytInitialData', 'ytInitialPlayerResponse',
        '__NEXT_DATA__', '__NUXT__', '__NUXT_DATA__',
        '__APOLLO_STATE__', '__REDUX_STATE__', '__PRELOADED_STATE__',
        '__INITIAL_STATE__', '__APP_STATE__',
        '_sharedData', '__initialData'
    ];
    var found = [];
    for (var i = 0; i < known.length; i++) {
        try {
            var val = window[known[i]];
            if (val && typeof val === 'object') {
                var size = JSON.stringify(val).length;
                found.push({name: known[i], size: size});
            }
        } catch(e) {}
    }

    // Scan window for large unknown objects (>10KB)
    var scanned = 0;
    try {
        var wkeys = Object.keys(window);
        for (var i = 0; i < wkeys.length && scanned < 200; i++) {
            var k = wkeys[i];
            if (known.indexOf(k) !== -1) continue;
            if (k.startsWith('__') || k.startsWith('_')) {
                scanned++;
                try {
                    var v = window[k];
                    if (v && typeof v === 'object' && !Array.isArray(v)) {
                        var s = JSON.stringify(v).length;
                        if (s > 10000) {
                            found.push({name: k, size: s});
                        }
                    }
                } catch(e) {}
            }
        }
    } catch(e) {}

    return found;
})()
"""

# ---------------------------------------------------------------------------
# Stage 3: Bayesian Model (pure Python)
# ---------------------------------------------------------------------------

# Strategy names
STRATEGIES = [
    "innerText", "host_attrs", "js_global", "react_fiber",
    "img_alt", "heading_hier", "data_testid", "shadow_pierce",
]

# Prior probabilities (must sum to 1.0)
# Rebalanced from 100-site batch: data_testid and js_global were underweighted
PRIORS = {
    "innerText":     0.35,
    "host_attrs":    0.08,
    "js_global":     0.15,
    "react_fiber":   0.08,
    "img_alt":       0.07,
    "heading_hier":  0.07,
    "data_testid":   0.15,
    "shadow_pierce": 0.05,
}

# Framework internals that inflate js_global_found — not real data stores
DATA_STORE_NOISE = {
    "__SENTRY__", "__BUILD_MANIFEST", "_sentryDebugIds", "_debugIds",
    "_vike", "_growthbook", "___jsl", "_aape", "_bcq",
    "__visibleCallbackList", "__STATSIG__", "__SECRET_LIGHTS__",
    "__componentIslands",
}

# Minimum store size (bytes) for unknown stores to count as data-bearing
MIN_DATA_STORE_SIZE = 50_000

# Likelihood table: P(observation | strategy)
# LIKELIHOODS[feature][observation][strategy] = probability
LIKELIHOODS = {
    "shadow_roots": {
        "high": {  # >50
            "innerText": 0.05, "host_attrs": 0.90, "js_global": 0.10,
            "react_fiber": 0.05, "img_alt": 0.05, "heading_hier": 0.05,
            "data_testid": 0.10, "shadow_pierce": 0.85,
        },
        "medium": {  # >5
            "innerText": 0.10, "host_attrs": 0.50, "js_global": 0.15,
            "react_fiber": 0.10, "img_alt": 0.10, "heading_hier": 0.10,
            "data_testid": 0.15, "shadow_pierce": 0.60,
        },
        "low": {  # <=5
            "innerText": 0.85, "host_attrs": 0.10, "js_global": 0.75,
            "react_fiber": 0.85, "img_alt": 0.85, "heading_hier": 0.85,
            "data_testid": 0.75, "shadow_pierce": 0.05,
        },
    },
    "custom_elements": {
        "high": {  # >30
            "innerText": 0.05, "host_attrs": 0.85, "js_global": 0.30,
            "react_fiber": 0.05, "img_alt": 0.10, "heading_hier": 0.05,
            "data_testid": 0.20, "shadow_pierce": 0.80,
        },
        "medium": {  # >5
            "innerText": 0.15, "host_attrs": 0.40, "js_global": 0.30,
            "react_fiber": 0.15, "img_alt": 0.15, "heading_hier": 0.10,
            "data_testid": 0.30, "shadow_pierce": 0.40,
        },
        "none": {  # 0
            "innerText": 0.80, "host_attrs": 0.05, "js_global": 0.40,
            "react_fiber": 0.80, "img_alt": 0.75, "heading_hier": 0.85,
            "data_testid": 0.50, "shadow_pierce": 0.05,
        },
    },
    "rich_attrs": {
        "high": {  # richAttrEls>5 AND avgAttrs>15
            "innerText": 0.05, "host_attrs": 0.95, "js_global": 0.10,
            "react_fiber": 0.05, "img_alt": 0.05, "heading_hier": 0.05,
            "data_testid": 0.10, "shadow_pierce": 0.30,
        },
        "low": {
            "innerText": 0.95, "host_attrs": 0.05, "js_global": 0.90,
            "react_fiber": 0.95, "img_alt": 0.95, "heading_hier": 0.95,
            "data_testid": 0.90, "shadow_pierce": 0.70,
        },
    },
    "js_global_found": {
        True: {
            "innerText": 0.10, "host_attrs": 0.10, "js_global": 0.95,
            "react_fiber": 0.15, "img_alt": 0.10, "heading_hier": 0.10,
            "data_testid": 0.15, "shadow_pierce": 0.10,
        },
        False: {
            "innerText": 0.90, "host_attrs": 0.90, "js_global": 0.05,
            "react_fiber": 0.85, "img_alt": 0.90, "heading_hier": 0.90,
            "data_testid": 0.85, "shadow_pierce": 0.90,
        },
    },
    "react_fiber": {
        True: {
            "innerText": 0.20, "host_attrs": 0.05, "js_global": 0.30,
            "react_fiber": 0.95, "img_alt": 0.20, "heading_hier": 0.20,
            "data_testid": 0.40, "shadow_pierce": 0.05,
        },
        False: {
            "innerText": 0.80, "host_attrs": 0.95, "js_global": 0.70,
            "react_fiber": 0.05, "img_alt": 0.80, "heading_hier": 0.80,
            "data_testid": 0.60, "shadow_pierce": 0.95,
        },
    },
    "data_testids": {
        "very_many": {  # >100 — near-certain data_testid signal
            "innerText": 0.05, "host_attrs": 0.05, "js_global": 0.10,
            "react_fiber": 0.15, "img_alt": 0.05, "heading_hier": 0.05,
            "data_testid": 0.98, "shadow_pierce": 0.05,
        },
        "many": {  # >20
            "innerText": 0.10, "host_attrs": 0.10, "js_global": 0.15,
            "react_fiber": 0.30, "img_alt": 0.10, "heading_hier": 0.10,
            "data_testid": 0.95, "shadow_pierce": 0.10,
        },
        "some": {  # >3
            "innerText": 0.30, "host_attrs": 0.20, "js_global": 0.25,
            "react_fiber": 0.40, "img_alt": 0.20, "heading_hier": 0.20,
            "data_testid": 0.60, "shadow_pierce": 0.15,
        },
        "none": {  # 0
            "innerText": 0.60, "host_attrs": 0.70, "js_global": 0.60,
            "react_fiber": 0.30, "img_alt": 0.70, "heading_hier": 0.70,
            "data_testid": 0.05, "shadow_pierce": 0.75,
        },
    },
    "total_elements": {
        "sparse": {  # <200
            "innerText": 0.90, "host_attrs": 0.05, "js_global": 0.20,
            "react_fiber": 0.20, "img_alt": 0.30, "heading_hier": 0.40,
            "data_testid": 0.10, "shadow_pierce": 0.05,
        },
        "normal": {  # 200-1500
            "innerText": 0.50, "host_attrs": 0.40, "js_global": 0.50,
            "react_fiber": 0.50, "img_alt": 0.50, "heading_hier": 0.40,
            "data_testid": 0.50, "shadow_pierce": 0.30,
        },
        "dense": {  # >1500
            "innerText": 0.10, "host_attrs": 0.55, "js_global": 0.30,
            "react_fiber": 0.30, "img_alt": 0.20, "heading_hier": 0.20,
            "data_testid": 0.40, "shadow_pierce": 0.65,
        },
    },
}


def categorize_features(fingerprint, stores=None):
    """Convert raw fingerprint counts into categorical observations.

    Returns a dict of {feature_name: observation_value} for Bayesian conditioning.
    """
    obs = {}

    # shadow_roots
    sr = fingerprint.get("shadowRoots", 0)
    if sr > 50:
        obs["shadow_roots"] = "high"
    elif sr > 5:
        obs["shadow_roots"] = "medium"
    else:
        obs["shadow_roots"] = "low"

    # custom_elements
    ce = fingerprint.get("customEls", 0)
    if ce > 30:
        obs["custom_elements"] = "high"
    elif ce > 5:
        obs["custom_elements"] = "medium"
    else:
        obs["custom_elements"] = "none"

    # rich_attrs
    ra_els = fingerprint.get("richAttrEls", 0)
    ra_avg = fingerprint.get("avgAttrs", 0)
    if ra_els > 5 and ra_avg > 15:
        obs["rich_attrs"] = "high"
    else:
        obs["rich_attrs"] = "low"

    # js_global_found — filter out framework noise
    data_stores = []
    if stores:
        for s in stores:
            name = s.get("name", "")
            size = s.get("size", 0)
            if name in DATA_STORE_NOISE:
                continue
            # Known data stores count at any size; unknown need >50KB
            if name in ("ytInitialData", "ytInitialPlayerResponse",
                        "__REDUX_STATE__", "__APOLLO_STATE__",
                        "__PRELOADED_STATE__", "__INITIAL_STATE__",
                        "_sharedData", "__initialData",
                        "__NUXT__", "__NUXT_DATA__"):
                data_stores.append(s)
            elif size >= MIN_DATA_STORE_SIZE:
                data_stores.append(s)
            elif name == "__NEXT_DATA__" and size >= 5000:
                # __NEXT_DATA__ is only useful if it has real page data (>5KB)
                data_stores.append(s)
    obs["js_global_found"] = len(data_stores) > 0

    # react_fiber
    obs["react_fiber"] = bool(fingerprint.get("reactFiber", False))

    # data_testids
    dt = fingerprint.get("dataTestIds", 0)
    if dt > 100:
        obs["data_testids"] = "very_many"
    elif dt > 20:
        obs["data_testids"] = "many"
    elif dt > 3:
        obs["data_testids"] = "some"
    else:
        obs["data_testids"] = "none"

    # total_elements
    te = fingerprint.get("totalEls", 0)
    if te < 200:
        obs["total_elements"] = "sparse"
    elif te > 1500:
        obs["total_elements"] = "dense"
    else:
        obs["total_elements"] = "normal"

    return obs


def bayesian_rank(observations):
    """Naive Bayes ranking of strategies given feature observations.

    Returns list of (strategy, probability) sorted by probability descending.
    """
    # Start with priors (log-space for numerical stability)
    log_post = {}
    for s in STRATEGIES:
        log_post[s] = math.log(PRIORS[s])

    # Multiply by likelihoods
    for feature, observation in observations.items():
        if feature not in LIKELIHOODS:
            continue
        obs_table = LIKELIHOODS[feature]
        if observation not in obs_table:
            continue
        strategy_likes = obs_table[observation]
        for s in STRATEGIES:
            if s in strategy_likes:
                p = strategy_likes[s]
                # Clamp to avoid log(0)
                p = max(p, 1e-10)
                log_post[s] += math.log(p)

    # Normalize (convert from log-space)
    max_log = max(log_post.values())
    posteriors = {}
    for s in STRATEGIES:
        posteriors[s] = math.exp(log_post[s] - max_log)

    total = sum(posteriors.values())
    if total > 0:
        for s in STRATEGIES:
            posteriors[s] /= total

    ranked = sorted(posteriors.items(), key=lambda x: -x[1])
    return ranked


# ---------------------------------------------------------------------------
# Stage 4: Extraction strategy JS IIFEs
# ---------------------------------------------------------------------------

EXTRACT_HOST_ATTRS_JS = r"""
(function() {
    var all = document.querySelectorAll('*');
    var results = [];
    var seen = new Set();
    for (var i = 0; i < all.length && results.length < 30; i++) {
        var el = all[i];
        if (el.attributes.length < 8) continue;
        var tag = el.tagName.toLowerCase();
        if (tag.indexOf('-') === -1) continue;

        var attrs = {};
        for (var j = 0; j < el.attributes.length; j++) {
            var a = el.attributes[j];
            var v = a.value;
            if (v.length > 100) v = v.substring(0, 97) + '...';
            attrs[a.name] = v;
        }

        // Deduplicate by tag+first-attr-key combo
        var key = tag + ':' + Object.keys(attrs).sort().join(',');
        if (seen.has(key)) continue;
        seen.add(key);

        // Also grab direct text
        var txt = '';
        for (var c = 0; c < el.childNodes.length; c++) {
            if (el.childNodes[c].nodeType === 3)
                txt += el.childNodes[c].textContent;
        }
        txt = txt.trim().substring(0, 100);

        results.push({tag: tag, attrs: attrs, text: txt});
    }
    return results;
})()
"""

EXTRACT_JS_GLOBAL_JS = r"""
(function(globalName, maxDepth) {
    var val = window[globalName];
    if (!val) return {error: 'not found'};

    function mapShape(obj, depth) {
        if (depth > maxDepth) return '...';
        if (obj === null || obj === undefined) return null;
        if (typeof obj !== 'object') return typeof obj;
        if (Array.isArray(obj)) {
            if (obj.length === 0) return '[]';
            return ['[' + obj.length + ']', mapShape(obj[0], depth + 1)];
        }
        var result = {};
        var keys = Object.keys(obj);
        for (var i = 0; i < keys.length && i < 20; i++) {
            result[keys[i]] = mapShape(obj[keys[i]], depth + 1);
        }
        if (keys.length > 20) result['...'] = '+' + (keys.length - 20) + ' keys';
        return result;
    }

    return {name: globalName, size: JSON.stringify(val).length, shape: mapShape(val, 0)};
})(%s, %s)
"""

EXTRACT_INNERTEXT_JS = r"""
(function() {
    var text = document.body.innerText || '';
    // Collapse whitespace runs
    text = text.replace(/\n{3,}/g, '\n\n').replace(/[ \t]+/g, ' ').trim();
    return text.substring(0, 3000);
})()
"""

EXTRACT_IMG_ALT_JS = r"""
(function() {
    var imgs = document.querySelectorAll('img');
    var results = [];
    for (var i = 0; i < imgs.length && results.length < 30; i++) {
        var img = imgs[i];
        var alt = img.alt || '';
        var src = img.src || '';
        var r = img.getBoundingClientRect();
        if (r.width < 20 || r.height < 20) continue;  // skip tiny icons
        if (!alt && !src) continue;

        var entry = {};
        if (alt) entry.alt = alt.substring(0, 100);
        if (src) {
            // Extract filename from src
            try {
                var fname = new URL(src).pathname.split('/').pop();
                if (fname) entry.src = fname.substring(0, 60);
            } catch(e) {
                entry.src = src.substring(0, 60);
            }
        }
        entry.size = Math.round(r.width) + 'x' + Math.round(r.height);
        results.push(entry);
    }
    return results;
})()
"""

EXTRACT_HEADING_HIER_JS = r"""
(function() {
    var headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6');
    var results = [];
    for (var i = 0; i < headings.length && results.length < 30; i++) {
        var h = headings[i];
        var text = h.textContent.trim().substring(0, 100);
        if (!text) continue;
        var tag = h.tagName.toLowerCase();
        var st = window.getComputedStyle(h);
        var fontSize = parseFloat(st.fontSize) || 0;
        results.push({level: tag, text: text, fontSize: Math.round(fontSize)});
    }

    // Check for overlay/modal
    var overlays = document.querySelectorAll('[class*="overlay"], [class*="modal"], [role="dialog"]');
    var hasOverlay = false;
    for (var i = 0; i < overlays.length; i++) {
        var st = window.getComputedStyle(overlays[i]);
        if (st.display !== 'none' && st.visibility !== 'hidden') {
            hasOverlay = true;
            break;
        }
    }

    return {headings: results, hasOverlay: hasOverlay, iframes: document.querySelectorAll('iframe').length};
})()
"""

EXTRACT_DATA_TESTID_JS = r"""
(function() {
    var els = document.querySelectorAll('[data-testid]');
    var results = [];
    var seen = new Set();
    for (var i = 0; i < els.length && results.length < 40; i++) {
        var el = els[i];
        var tid = el.getAttribute('data-testid');
        if (seen.has(tid)) continue;
        seen.add(tid);

        var text = el.textContent.trim().substring(0, 120);
        var tag = el.tagName.toLowerCase();
        results.push({testid: tid, tag: tag, text: text});
    }
    return results;
})()
"""

EXTRACT_SHADOW_PIERCE_JS = r"""
(function() {
    var all = document.querySelectorAll('*');
    var results = [];
    for (var i = 0; i < all.length && results.length < 20; i++) {
        var el = all[i];
        if (!el.shadowRoot) continue;
        var tag = el.tagName.toLowerCase();
        var children = el.shadowRoot.querySelectorAll('*');
        var textParts = [];
        for (var j = 0; j < children.length && textParts.length < 5; j++) {
            var txt = '';
            for (var c = 0; c < children[j].childNodes.length; c++) {
                if (children[j].childNodes[c].nodeType === 3)
                    txt += children[j].childNodes[c].textContent;
            }
            txt = txt.trim();
            if (txt.length > 5) textParts.push(txt.substring(0, 80));
        }
        if (textParts.length > 0) {
            results.push({host: tag, childCount: children.length, text: textParts});
        }
    }
    return results;
})()
"""

EXTRACT_REACT_FIBER_JS = r"""
(function() {
    var candidates = [
        document.getElementById('root'),
        document.getElementById('__next'),
        document.getElementById('app'),
        document.getElementById('__app'),
        document.getElementById('main'),
    ];
    if (document.body) {
        for (var ci = 0; ci < Math.min(document.body.children.length, 5); ci++) {
            candidates.push(document.body.children[ci]);
        }
    }

    var rootEl = null, fiberKey = null;
    for (var ci = 0; ci < candidates.length; ci++) {
        var el = candidates[ci];
        if (!el) continue;
        var keys = Object.keys(el);
        for (var ki = 0; ki < keys.length; ki++) {
            if (keys[ki].indexOf('__reactFiber') === 0 || keys[ki].indexOf('__reactInternalInstance') === 0) {
                rootEl = el;
                fiberKey = keys[ki];
                break;
            }
        }
        if (fiberKey) break;
    }
    if (!rootEl) return {error: 'no root element'};
    if (!fiberKey) return {error: 'no fiber key'};

    // Walk fiber tree, collect memoizedProps from first 30 components
    var fiber = rootEl[fiberKey];
    var results = [];
    var queue = [fiber];
    var visited = 0;
    while (queue.length > 0 && results.length < 30 && visited < 200) {
        var node = queue.shift();
        if (!node) continue;
        visited++;

        if (node.memoizedProps && typeof node.memoizedProps === 'object') {
            var props = node.memoizedProps;
            var propKeys = Object.keys(props).filter(function(k) {
                return k !== 'children' && typeof props[k] !== 'function';
            });
            if (propKeys.length > 0) {
                var entry = {type: (node.type && node.type.name) || (node.type && node.type.displayName) || String(node.type || '?').substring(0, 30)};
                var p = {};
                for (var j = 0; j < propKeys.length && j < 10; j++) {
                    var v = props[propKeys[j]];
                    if (typeof v === 'object' && v !== null) {
                        p[propKeys[j]] = Array.isArray(v) ? '[' + v.length + ']' : '{...}';
                    } else {
                        p[propKeys[j]] = String(v).substring(0, 80);
                    }
                }
                entry.props = p;
                results.push(entry);
            }
        }

        if (node.child) queue.push(node.child);
        if (node.sibling) queue.push(node.sibling);
    }
    return {fiberKey: fiberKey, components: results};
})()
"""

# Map Shape utility (not a strategy, but a reusable JS primitive)
MAP_SHAPE_JS = r"""
(function(globalName, maxDepth) {
    var val = window[globalName];
    if (!val) return {error: 'global "' + globalName + '" not found'};

    function mapShape(obj, depth) {
        if (depth > maxDepth) return '...';
        if (obj === null || obj === undefined) return null;
        if (typeof obj !== 'object') return typeof obj;
        if (Array.isArray(obj)) {
            if (obj.length === 0) return '[]';
            return ['[' + obj.length + ']', mapShape(obj[0], depth + 1)];
        }
        var result = {};
        var keys = Object.keys(obj);
        for (var i = 0; i < keys.length && i < 20; i++) {
            result[keys[i]] = mapShape(obj[keys[i]], depth + 1);
        }
        if (keys.length > 20) result['...'] = '+' + (keys.length - 20) + ' keys';
        return result;
    }

    return {name: globalName, size: JSON.stringify(val).length, shape: mapShape(val, 0)};
})(%s, %s)
"""

# Find Paths utility — search object tree for keys matching a pattern
FIND_PATHS_JS = r"""
(function(globalName, keyPattern) {
    var val = window[globalName];
    if (!val) return {error: 'global "' + globalName + '" not found'};

    var results = [];
    var pat = keyPattern.toLowerCase();

    function walk(obj, path, depth) {
        if (depth > 6 || results.length >= 20) return;
        if (obj === null || obj === undefined || typeof obj !== 'object') return;

        var keys = Object.keys(obj);
        for (var i = 0; i < keys.length && results.length < 20; i++) {
            var k = keys[i];
            var newPath = path + '.' + k;
            if (k.toLowerCase().indexOf(pat) !== -1) {
                var v = obj[k];
                var preview;
                if (typeof v === 'string') preview = v.substring(0, 100);
                else if (typeof v === 'number' || typeof v === 'boolean') preview = String(v);
                else if (Array.isArray(v)) preview = '[' + v.length + ' items]';
                else if (v && typeof v === 'object') preview = '{' + Object.keys(v).length + ' keys}';
                else preview = String(v);
                results.push({path: newPath, preview: preview});
            }
            if (typeof obj[k] === 'object' && obj[k] !== null) {
                walk(obj[k], newPath, depth + 1);
            }
        }
    }

    walk(val, globalName, 0);
    return {global: globalName, pattern: keyPattern, matches: results};
})(%s, %s)
"""

# Strategy name → JS constant mapping
STRATEGY_JS = {
    "host_attrs":    EXTRACT_HOST_ATTRS_JS,
    "js_global":     EXTRACT_JS_GLOBAL_JS,
    "innerText":     EXTRACT_INNERTEXT_JS,
    "img_alt":       EXTRACT_IMG_ALT_JS,
    "heading_hier":  EXTRACT_HEADING_HIER_JS,
    "data_testid":   EXTRACT_DATA_TESTID_JS,
    "shadow_pierce": EXTRACT_SHADOW_PIERCE_JS,
    "react_fiber":   EXTRACT_REACT_FIBER_JS,
}


# ---------------------------------------------------------------------------
# Stage 5: Render + compress (pure Python)
# ---------------------------------------------------------------------------

def _format_size(size_bytes):
    """Format byte size as human-readable string."""
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.1f}MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.1f}KB"
    return f"{size_bytes}B"


def render_fingerprint(fingerprint, stores=None):
    """One-liner fingerprint summary (~20 tokens)."""
    fp = fingerprint
    parts = [
        f"{fp.get('totalEls', 0)}els",
        f"{fp.get('customEls', 0)}custom",
        f"{fp.get('shadowRoots', 0)}shadow",
        f"{fp.get('avgAttrs', 0)}avg-attrs",
        f"{fp.get('dataTestIds', 0)}testids",
        f"react:{'yes' if fp.get('reactFiber') else 'no'}",
    ]
    if stores:
        store_parts = []
        for s in stores[:3]:
            store_parts.append(f"{s['name']}({_format_size(s['size'])})")
        parts.append("stores:" + ",".join(store_parts))
    else:
        parts.append("stores:none")
    return "fingerprint: " + " ".join(parts)


def render_strategy_line(ranked):
    """Strategy ranking one-liner (~10 tokens)."""
    if not ranked:
        return "strategy: unknown"
    top = ranked[0]
    line = f"strategy: {top[0]} ({top[1]:.0%})"
    if len(ranked) > 1:
        runner = ranked[1]
        line += f" | runner-up: {runner[0]} ({runner[1]:.0%})"
    return line


def schema_rows_compress(items, key_order=None, max_rows=15):
    """Compress a list of dicts into schema+rows format.

    Returns a string like:
        schema: [author, title, score]
        user1 | Post title | 100
        user2 | Another post | 50
    """
    if not items:
        return "(no data)"

    # Determine column order
    if key_order:
        cols = key_order
    else:
        # Use keys from first item, sorted
        cols = sorted(items[0].keys()) if items else []

    lines = ["schema: [" + ", ".join(cols) + "]"]
    for item in items[:max_rows]:
        vals = []
        for col in cols:
            v = item.get(col, "")
            if isinstance(v, dict):
                v = "{...}"
            elif isinstance(v, list):
                v = f"[{len(v)}]"
            else:
                v = str(v)
            if len(v) > 60:
                v = v[:57] + "..."
            vals.append(v)
        lines.append(" | ".join(vals))

    if len(items) > max_rows:
        lines.append(f"... +{len(items) - max_rows} more")
    return "\n".join(lines)


def render_host_attrs(data):
    """Render host_attrs extraction as schema+rows."""
    if not data:
        return "(no host attributes found)"

    # Flatten: each item has tag + attrs dict + text
    items = []
    for entry in data[:15]:
        flat = {"tag": entry.get("tag", "")}
        attrs = entry.get("attrs", {})
        # Pick most informative attrs
        for key in sorted(attrs.keys()):
            if key in ("class", "style"):
                continue
            flat[key] = attrs[key]
        if entry.get("text"):
            flat["text"] = entry["text"]
        items.append(flat)

    if not items:
        return "(no host attributes found)"

    # Find common keys across items for schema
    all_keys = set()
    for item in items:
        all_keys.update(item.keys())
    # Prioritize: tag first, then sorted
    priority = ["tag", "text"]
    ordered = [k for k in priority if k in all_keys]
    ordered += sorted(k for k in all_keys if k not in priority)
    return schema_rows_compress(items, key_order=ordered[:8])


def render_js_global(data):
    """Render JS global shape as indented key-value tree."""
    if not data:
        return "(error: unknown)"
    if data.get("error"):
        return f"(error: {data['error']})"

    lines = [f"{data['name']}: {_format_size(data.get('size', 0))}"]

    def _render_shape(shape, indent=1):
        prefix = "  " * indent
        if isinstance(shape, dict):
            for k, v in list(shape.items())[:15]:
                if isinstance(v, dict):
                    lines.append(f"{prefix}{k}:")
                    _render_shape(v, indent + 1)
                elif isinstance(v, list):
                    lines.append(f"{prefix}{k}: {v}")
                else:
                    lines.append(f"{prefix}{k}: {v}")
        elif isinstance(shape, list):
            lines.append(f"{prefix}{shape}")
        else:
            lines.append(f"{prefix}{shape}")

    _render_shape(data.get("shape", {}))
    return "\n".join(lines)


def render_innertext(data):
    """Render innerText extraction (already compressed by JS)."""
    if not data:
        return "(empty page)"
    text = str(data)
    if len(text) > 2000:
        text = text[:2000] + f"\n... +{len(data) - 2000} chars"
    return text


def render_img_alt(data):
    """Render image alt text extraction as schema+rows."""
    if not data:
        return "(no images found)"
    return schema_rows_compress(data, key_order=["alt", "src", "size"])


def render_heading_hier(data):
    """Render heading hierarchy."""
    if not data:
        return "(no headings)"

    lines = []
    headings = data.get("headings", [])
    if data.get("hasOverlay"):
        lines.append("[OVERLAY/MODAL DETECTED]")
    if data.get("iframes", 0) > 0:
        lines.append(f"iframes: {data['iframes']}")

    for h in headings[:20]:
        level = h.get("level", "h?")
        indent = "  " * (int(level[1]) - 1) if level[1:].isdigit() else ""
        lines.append(f"{indent}{level}: {h.get('text', '')}")

    return "\n".join(lines) if lines else "(no headings)"


def render_data_testid(data):
    """Render data-testid extraction as schema+rows."""
    if not data:
        return "(no data-testid elements)"
    return schema_rows_compress(data, key_order=["testid", "tag", "text"])


def render_shadow_pierce(data):
    """Render shadow DOM pierce results."""
    if not data:
        return "(no shadow DOM content)"

    lines = []
    for entry in data[:10]:
        host = entry.get("host", "?")
        count = entry.get("childCount", 0)
        lines.append(f"<{host}> ({count} children)")
        for txt in entry.get("text", [])[:3]:
            lines.append(f"  {txt}")
    return "\n".join(lines) if lines else "(no shadow DOM content)"


def render_react_fiber(data):
    """Render React fiber tree walk."""
    if not data or data.get("error"):
        return f"(error: {data.get('error', 'unknown')})"

    lines = [f"fiber: {data.get('fiberKey', '?')}"]
    for comp in data.get("components", [])[:15]:
        ctype = comp.get("type", "?")
        props = comp.get("props", {})
        prop_str = " ".join(f"{k}={v}" for k, v in list(props.items())[:5])
        lines.append(f"  <{ctype}> {prop_str}")
    return "\n".join(lines)


def render_find_paths(data):
    """Render find-paths results."""
    if not data or data.get("error"):
        return f"(error: {data.get('error', 'unknown')})"

    lines = [f"find: {data.get('pattern', '?')} in {data.get('global', '?')}"]
    for match in data.get("matches", [])[:20]:
        lines.append(f"  {match['path']}: {match.get('preview', '?')}")
    if not data.get("matches"):
        lines.append("  (no matches)")
    return "\n".join(lines)


def render_stores(stores):
    """Render store listing."""
    if not stores:
        return "stores: none found"
    lines = ["stores:"]
    for s in stores:
        lines.append(f"  {s['name']}: {_format_size(s['size'])}")
    return "\n".join(lines)


# Strategy name → render function
STRATEGY_RENDERERS = {
    "host_attrs":    render_host_attrs,
    "js_global":     render_js_global,
    "innerText":     render_innertext,
    "img_alt":       render_img_alt,
    "heading_hier":  render_heading_hier,
    "data_testid":   render_data_testid,
    "shadow_pierce": render_shadow_pierce,
    "react_fiber":   render_react_fiber,
}


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def parse_args(args):
    """Parse CLI arguments. Returns a dict of parsed options."""
    opts = {
        "mode": None,        # probe, extract, shape, find_paths, stores
        "strategy": None,    # forced strategy name
        "url": None,
        "tab_id": None,
        "new_tab": False,
        "list_tabs": False,
        "close_tab_id": None,
        "shape_global": None,     # global name for --shape
        "shape_depth": 3,         # depth for --shape
        "find_global": None,      # global name for --find-paths
        "find_pattern": None,     # key pattern for --find-paths
    }

    # --help / -h
    if '--help' in args or '-h' in args:
        print("""Usage: intel [options] [url]

Options:
  --port <int>          Chrome debug port (default: 9222)
  --tab <id>            Use specific tab (prefix match OK)
  --tabs                List open tabs and exit
  --new                 Open new tab
  --close <id>          Close tab
  --probe               Fingerprint + strategy ranking (default)
  --extract             Full pipeline: probe -> extract data
  --strategy <name>     Force extraction strategy
  --stores              List JS data stores
  --shape <global>      Get shape of JS global variable
  --depth <int>         Shape traversal depth (default 3)
  --find-paths <g> <p>  Find key paths in global matching pattern
  -h, --help            Show this help""")
        sys.exit(0)

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--probe":
            opts["mode"] = "probe"
            i += 1
        elif a == "--extract":
            opts["mode"] = "extract"
            i += 1
        elif a == "--stores":
            opts["mode"] = "stores"
            i += 1
        elif a == "--strategy" and i + 1 < len(args):
            opts["strategy"] = args[i + 1]
            i += 2
        elif a == "--shape" and i + 1 < len(args):
            opts["mode"] = "shape"
            opts["shape_global"] = args[i + 1]
            i += 2
        elif a == "--depth" and i + 1 < len(args):
            opts["shape_depth"] = int(args[i + 1])
            i += 2
        elif a == "--find-paths" and i + 2 < len(args):
            opts["mode"] = "find_paths"
            opts["find_global"] = args[i + 1]
            opts["find_pattern"] = args[i + 2]
            i += 3
        elif a == "--port" and i + 1 < len(args):
            global _CDP_PORT
            _CDP_PORT = int(args[i + 1])
            i += 2
        elif a == "--tab" and i + 1 < len(args):
            val = args[i + 1]
            opts["tab_id"] = None if val == "auto" else val
            i += 2
        elif a == "--tabs":
            opts["list_tabs"] = True
            i += 1
        elif a == "--new":
            opts["new_tab"] = True
            i += 1
        elif a == "--close" and i + 1 < len(args):
            opts["close_tab_id"] = args[i + 1]
            i += 2
        elif not a.startswith("-"):
            opts["url"] = a
            i += 1
        else:
            print(f"ERROR: Unknown flag '{a}'. Run with --help for usage.", file=sys.stderr)
            sys.exit(1)

    return opts


# ---------------------------------------------------------------------------
# Main: CDP connect, probe, extract, render
# ---------------------------------------------------------------------------

async def run(args):
    opts = parse_args(args)

    _ensure_chrome()

    # --tabs: list available tabs and exit
    if opts["list_tabs"]:
        tabs = _list_tabs()
        print(f"=== CDP Tabs ({len(tabs)}) ===")
        for t in tabs:
            tid = t["id"]
            title = t.get("title", "")[:50]
            turl = t.get("url", "")[:80]
            print(f"  {tid}  {title or '(empty)'}  {turl}")
        return

    # --close: close a tab and exit
    if opts["close_tab_id"]:
        if _close_tab(opts["close_tab_id"]):
            print(f"Closed tab {opts['close_tab_id']}")
        else:
            print(f"Failed to close tab {opts['close_tab_id']}", file=sys.stderr)
        return

    # Resolve which tab to connect to
    tab_id = opts["tab_id"]
    if opts["new_tab"]:
        tab_info = _create_new_tab()
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
        if opts["url"]:
            await cdp.navigate(opts["url"], wait=3)

        # Get page title and URL in one CDP round-trip
        page_title, page_url = await cdp.get_page_info()

        mode = opts["mode"] or "probe"

        # --shape mode
        if mode == "shape":
            js = MAP_SHAPE_JS % (json.dumps(opts["shape_global"]), int(opts["shape_depth"]))
            result = await cdp.execute_js(js)
            data = result.get("result", {}).get("value")
            print(f"Tab: {tab_id}")
            print(f"Page: {page_title}")
            print(render_js_global(data))
            return

        # --find-paths mode
        if mode == "find_paths":
            js = FIND_PATHS_JS % (json.dumps(opts["find_global"]), json.dumps(opts["find_pattern"]))
            result = await cdp.execute_js(js)
            data = result.get("result", {}).get("value")
            print(f"Tab: {tab_id}")
            print(f"Page: {page_title}")
            print(render_find_paths(data))
            return

        # --stores mode
        if mode == "stores":
            result = await cdp.execute_js(STORE_PROBE_JS)
            stores = result.get("result", {}).get("value", [])
            print(f"Tab: {tab_id}")
            print(f"Page: {page_title}")
            print(render_stores(stores))
            return

        # Stages 1+2: Fingerprint + store probe in 1 round-trip (saves ~80ms over tunnel)
        _batch = await cdp.batch_evaluate([FINGERPRINT_JS, STORE_PROBE_JS])
        fingerprint = _batch[0].get("result", {}).get("value", {})
        stores = _batch[1].get("result", {}).get("value", [])

        # Stage 3: Categorize + Bayesian rank
        observations = categorize_features(fingerprint, stores)
        ranked = bayesian_rank(observations)

        # Output header
        output_lines = [f"Tab: {tab_id}", f"Page: {page_title}"]
        if page_url:
            output_lines.append(f"URL: {page_url}")

        output_lines.append(render_fingerprint(fingerprint, stores))
        output_lines.append(render_strategy_line(ranked))

        if mode == "probe":
            print("\n".join(output_lines))
            return

        # --extract mode: execute the top strategy
        strategy = opts["strategy"] or ranked[0][0]
        if strategy not in STRATEGY_JS:
            print(f"Error: unknown strategy '{strategy}'", file=sys.stderr)
            return

        js = STRATEGY_JS[strategy]

        # Special handling for js_global strategy — needs the global name
        if strategy == "js_global":
            if stores:
                global_name = stores[0]["name"]
            else:
                global_name = "window"
            js = EXTRACT_JS_GLOBAL_JS % (json.dumps(global_name), 3)

        result = await cdp.execute_js(js)
        data = result.get("result", {}).get("value")

        # Render the extraction result
        renderer = STRATEGY_RENDERERS.get(strategy, render_innertext)
        rendered = renderer(data)

        output_lines.append(f"--- {strategy} ---")
        output_lines.append(rendered)
        print("\n".join(output_lines))

    finally:
        if cdp.ws:
            await cdp.ws.close()


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
