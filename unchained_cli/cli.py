"""unchained CLI — browser automation over local Chrome CDP.

Usage:
    unchained [--port PORT] [--tab TAB] <command> [args]

Global options:
    --port PORT     Chrome remote debugging port (default: 9222, env: UNCHAINED_PORT)
    --tab  TAB_ID   Target tab ID or 'auto' for the first page tab (default: auto)
    --json          Output raw JSON where applicable

Commands:
    tabs                         List open tabs
    navigate <url>               Navigate to URL
    click  --x X --y Y           Click at coordinates
    click  --selector CSS        Click element by CSS selector
    type   <text>                Type text into focused element
    scroll [--direction DIR]     Scroll page (up/down/left/right, default: down)
           [--amount N]          Pixels to scroll (default: 500)
    screenshot [--output FILE]   Save screenshot (default: screenshot.png)
    js     <expression>          Evaluate JavaScript and print result
    key    <key>                 Press a key (Enter, Tab, Escape, ArrowDown, …)
           [--modifiers N]       Modifier bitmask: 1=Alt 2=Ctrl 4=Meta 8=Shift
    wait   [--strategy STRAT]    Wait for page load (dom/network/both, default: both)
           [--timeout N]         Timeout in seconds (default: 30)
    cookies get [--urls URL ...] Get cookies for URLs
    cookies set <json>           Inject cookies from JSON array
    frames                       List iframes on the page
    ddm    [flags ...]           DOM Density Map (requires ddm binary)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn

from .chrome import ChromeClient, CDPError
from . import ddm as _ddm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> NoReturn:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _print_result(value, as_json: bool = False) -> None:
    if value is None:
        return
    if as_json:
        print(json.dumps(value, indent=2))
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2))
    else:
        print(value)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_tabs(client: ChromeClient, args: argparse.Namespace) -> None:
    tabs = client.list_tabs()
    if not tabs:
        print("No page tabs open.")
        return
    if args.json:
        print(json.dumps(tabs, indent=2))
        return
    for i, t in enumerate(tabs):
        marker = " *" if i == 0 else "  "
        title = t.get("title", "")[:60]
        url   = t.get("url",   "")[:80]
        print(f"{marker} [{t['id']}]  {title}")
        print(f"      {url}")


def cmd_navigate(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    result = client.navigate(tab_id, args.url)
    final = client.js_eval(tab_id, "window.location.href")
    if not isinstance(final, str) or not final:
        final = args.url
    if args.json:
        print(json.dumps({
            "tab_id": tab_id,
            "url": final,
            "navigation": result,
        }, indent=2))
        return
    print(f"Navigated → {final}")


def cmd_click(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    if args.selector:
        pos = client.click_selector(tab_id, args.selector)
        print(f"Clicked selector {args.selector!r} at ({int(pos['x'])}, {int(pos['y'])})")
    elif args.x is not None and args.y is not None:
        client.click(tab_id, args.x, args.y)
        print(f"Clicked ({args.x}, {args.y})")
    else:
        _die("Provide --x X --y Y or --selector CSS")


def cmd_type(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    client.type_text(tab_id, args.text)
    preview = args.text[:40] + ("…" if len(args.text) > 40 else "")
    print(f"Typed: {preview!r}")


def cmd_scroll(client: ChromeClient, args: argparse.Namespace) -> None:
    direction = args.direction or "down"
    amount    = args.amount    or 500
    if direction not in ("up", "down", "left", "right"):
        _die(f"Invalid direction {direction!r}. Use up/down/left/right.")
    amount = max(1, min(amount, 5000))
    tab_id = client.resolve_tab(args.tab)
    client.scroll(tab_id, direction, amount)
    print(f"Scrolled {direction} {amount}px")


def cmd_screenshot(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    png = client.screenshot(tab_id)
    output = args.output or "screenshot.png"
    with open(output, "wb") as f:
        f.write(png)
    print(f"Screenshot saved → {output}  ({len(png):,} bytes)")


def cmd_js(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    result = client.js_eval(tab_id, args.expression)
    _print_result(result, as_json=args.json)


def cmd_key(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    client.key_press(tab_id, args.key, args.modifiers or 0)
    print(f"Key pressed: {args.key} (modifiers={args.modifiers or 0})")


def cmd_wait(client: ChromeClient, args: argparse.Namespace) -> None:
    strategy = args.strategy or "both"
    timeout  = args.timeout  or 30.0
    if strategy not in ("dom", "network", "both"):
        _die(f"Invalid strategy {strategy!r}. Use dom/network/both.")
    tab_id = client.resolve_tab(args.tab)
    client.wait_ready(tab_id, strategy, timeout)
    print(f"Page ready (strategy={strategy})")


def cmd_cookies_get(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    urls   = args.urls or None
    cookies = client.get_cookies(tab_id, urls)
    if args.json:
        print(json.dumps(cookies, indent=2))
    else:
        for c in cookies:
            print(f"  {c.get('name')}={c.get('value')[:40]}  domain={c.get('domain')}")
        print(f"({len(cookies)} cookies)")


def cmd_cookies_set(client: ChromeClient, args: argparse.Namespace) -> None:
    try:
        cookie_list = json.loads(args.cookie_json)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}")
    if not isinstance(cookie_list, list):
        _die("Expected a JSON array of cookie objects.")
    tab_id = client.resolve_tab(args.tab)
    client.set_cookies(tab_id, cookie_list)
    print(f"Set {len(cookie_list)} cookie(s)")


def cmd_frames(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    frames = client.list_frames(tab_id)
    if args.json:
        print(json.dumps(frames, indent=2))
    else:
        for f in frames:
            print(f"  [{f['index']}] {f['frameId']}  {f['url'][:80]}")


def cmd_ddm(args: argparse.Namespace) -> None:
    code = _ddm.run_ddm(args.port, args.tab, args.ddm_flags)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unchained",
        description="Browser automation over local Chrome CDP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("UNCHAINED_PORT", 9222)),
        metavar="PORT",
        help="Chrome remote debugging port (default: 9222, env: UNCHAINED_PORT)",
    )
    parser.add_argument(
        "--tab", default="auto", metavar="TAB_ID",
        help="Target tab ID or 'auto' for the first page tab (default: auto)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON",
    )

    sub = parser.add_subparsers(dest="command", metavar="command")

    # tabs
    sub.add_parser("tabs", help="List open tabs")

    # navigate
    p = sub.add_parser("navigate", help="Navigate to URL")
    p.add_argument("url", help="Target URL")

    # click
    p = sub.add_parser("click", help="Click element or coordinates")
    p.add_argument("--x", type=int, help="X coordinate")
    p.add_argument("--y", type=int, help="Y coordinate")
    p.add_argument("--selector", metavar="CSS", help="CSS selector")

    # type
    p = sub.add_parser("type", help="Type text into focused element")
    p.add_argument("text", help="Text to type")

    # scroll
    p = sub.add_parser("scroll", help="Scroll the page")
    p.add_argument("--direction", choices=["up","down","left","right"],
                   default="down", help="Scroll direction (default: down)")
    p.add_argument("--amount", type=int, default=500,
                   help="Pixels to scroll (default: 500, max: 5000)")

    # screenshot
    p = sub.add_parser("screenshot", help="Take a screenshot")
    p.add_argument("--output", "-o", metavar="FILE",
                   help="Output file (default: screenshot.png)")

    # js
    p = sub.add_parser("js", help="Evaluate JavaScript")
    p.add_argument("expression", help="JS expression to evaluate")

    # key
    p = sub.add_parser("key", help="Press a keyboard key")
    p.add_argument("key", help="Key name (Enter, Tab, Escape, ArrowDown, a-z, 0-9, …)")
    p.add_argument("--modifiers", type=int, default=0,
                   help="Modifier bitmask: 1=Alt 2=Ctrl 4=Meta 8=Shift (default: 0)")

    # wait
    p = sub.add_parser("wait", help="Wait for page to finish loading")
    p.add_argument("--strategy", choices=["dom","network","both"], default="both")
    p.add_argument("--timeout", type=float, default=30.0, metavar="SECS")

    # cookies
    cookies_p = sub.add_parser("cookies", help="Cookie management")
    cookies_sub = cookies_p.add_subparsers(dest="cookies_command", metavar="action")

    cg = cookies_sub.add_parser("get", help="Get cookies")
    cg.add_argument("--urls", nargs="+", metavar="URL",
                    help="One or more URLs to filter by domain")

    cs = cookies_sub.add_parser("set", help="Inject cookies from JSON array")
    cs.add_argument("cookie_json", metavar="JSON",
                    help='JSON array, e.g. \'[{"name":"s","value":"x","domain":".ex.com"}]\'')

    # frames
    sub.add_parser("frames", help="List iframes on the current page")

    # ddm
    p = sub.add_parser("ddm", help="DOM Density Map (requires ddm binary)")
    p.add_argument("ddm_flags", nargs=argparse.REMAINDER,
                   help="Flags passed directly to ddm binary")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # DDM bypasses the Chrome client — it shells out to a binary
    if args.command == "ddm":
        cmd_ddm(args)
        return

    client = ChromeClient(port=args.port)

    try:
        match args.command:
            case "tabs":
                cmd_tabs(client, args)
            case "navigate":
                cmd_navigate(client, args)
            case "click":
                cmd_click(client, args)
            case "type":
                cmd_type(client, args)
            case "scroll":
                cmd_scroll(client, args)
            case "screenshot":
                cmd_screenshot(client, args)
            case "js":
                cmd_js(client, args)
            case "key":
                cmd_key(client, args)
            case "wait":
                cmd_wait(client, args)
            case "cookies":
                if args.cookies_command == "get":
                    cmd_cookies_get(client, args)
                elif args.cookies_command == "set":
                    cmd_cookies_set(client, args)
                else:
                    parser.parse_args(["cookies", "--help"])
            case "frames":
                cmd_frames(client, args)
            case _:
                parser.print_help()
                sys.exit(1)

    except CDPError as e:
        _die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
