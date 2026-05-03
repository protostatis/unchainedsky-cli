"""Stealth fingerprint overrides — inject via CDP to evade bot detection.

Overrides navigator.webdriver, screen dimensions, WebGL GPU strings,
outerWidth/outerHeight, and chrome.runtime stubs. Applied via
Page.addScriptToEvaluateOnNewDocument so it persists across navigations.
"""
from __future__ import annotations

import asyncio
import json
import random

import websockets

# GPU strings rotated per-session to avoid fingerprint clustering.
_WEBGL_GPUS = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580, OpenGL 4.6)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti, OpenGL 4.6)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
]


def _build_stealth_js() -> str:
    """Build stealth JS with a randomly selected GPU string."""
    vendor, renderer = random.choice(_WEBGL_GPUS)
    v_js = json.dumps(vendor)
    r_js = json.dumps(renderer)
    # Screen
    js = (
        'Object.defineProperty(screen,"width",{get:()=>1920});'
        'Object.defineProperty(screen,"height",{get:()=>1080});'
        'Object.defineProperty(screen,"availWidth",{get:()=>1920});'
        'Object.defineProperty(screen,"availHeight",{get:()=>1040});'
    )
    # Navigator
    js += (
        'Object.defineProperty(navigator,"deviceMemory",{get:()=>8});'
        'Object.defineProperty(navigator,"hardwareConcurrency",{get:()=>8});'
        'Object.defineProperty(navigator,"webdriver",{get:()=>undefined});'
    )
    # WebGL — fake context when headless returns null, override when real
    js += (
        'const _gc=HTMLCanvasElement.prototype.getContext;'
        'HTMLCanvasElement.prototype.getContext=function(t,a){'
        'const c=_gc.call(this,t,a);'
        'if(t==="webgl"||t==="webgl2"||t==="experimental-webgl"){'
        'if(!c)return{getExtension:n=>n==="WEBGL_debug_renderer_info"'
        '?{UNMASKED_VENDOR_WEBGL:0x9245,UNMASKED_RENDERER_WEBGL:0x9246}:null,'
        f'getParameter:p=>p===0x9245?{v_js}'
        f':p===0x9246?{r_js}:0,'
        'getSupportedExtensions:()=>["WEBGL_debug_renderer_info"],'
        'drawingBufferWidth:300,drawingBufferHeight:150,canvas:this};'
        'const _ge=c.getExtension.bind(c);const _gp=c.getParameter.bind(c);'
        'c.getExtension=n=>n==="WEBGL_debug_renderer_info"'
        '?{UNMASKED_VENDOR_WEBGL:0x9245,UNMASKED_RENDERER_WEBGL:0x9246}:_ge(n);'
        f'c.getParameter=p=>p===0x9245?{v_js}'
        f':p===0x9246?{r_js}:_gp(p)'
        '}return c};'
    )
    # outerWidth/outerHeight — CDP reports 0, bot detectors check >0
    js += (
        'Object.defineProperty(window,"outerWidth",'
        '{get:()=>window.innerWidth,configurable:true});'
        'Object.defineProperty(window,"outerHeight",'
        '{get:()=>window.innerHeight+85,configurable:true});'
    )
    # chrome.runtime stub
    js += (
        'if(window.chrome){window.chrome.runtime=window.chrome.runtime||{};'
        'window.chrome.runtime.connect=function(){};'
        'window.chrome.runtime.sendMessage=function(){}}'
    )
    return js


async def _inject(ws_url: str) -> None:
    """Connect to a Chrome tab and inject stealth overrides."""
    sid = random.randint(2**28, 2**30)

    async def _cdp(method, params=None):
        nonlocal sid
        sid += 1
        await ws.send(json.dumps(
            {"id": sid, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("id") == sid:
                return msg

    async with websockets.connect(ws_url, ping_timeout=None) as ws:
        await _cdp("Emulation.setDeviceMetricsOverride", {
            "width": 1920, "height": 1080, "deviceScaleFactor": 1,
            "mobile": False, "screenWidth": 1920, "screenHeight": 1080,
        })
        await _cdp("Page.addScriptToEvaluateOnNewDocument", {
            "source": _build_stealth_js(),
        })


def inject_stealth(host: str, port: int) -> None:
    """Inject stealth overrides into all page tabs on the given port."""
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/json", timeout=3
        ) as r:
            tabs = json.loads(r.read())
    except Exception:
        return

    for tab in tabs:
        if tab.get("type") != "page":
            continue
        if (tab.get("url") or "").startswith(("chrome://", "devtools://")):
            continue
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            asyncio.run(_inject(ws_url))
        except Exception:
            pass  # best-effort per tab
