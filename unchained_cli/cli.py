"""unchained CLI — browser automation over local Chrome CDP.

Usage:
    unchained [--port PORT] [--tab TAB] <command> [args]

Global options:
    --port PORT     Chrome remote debugging port (default: 9222, env: UNCHAINED_PORT)
    --tab  TAB_ID   Target tab ID, alias, or 'auto' (default: auto)
    --json          Output raw JSON where applicable

Commands:
    launch [url]                 Launch Chrome with hardened CDP startup
    status                       Check if Chrome is alive
    kill                         Kill Chrome on this port
    tabs                         List open tabs
    create_tab [url]             Open a new tab
    close_tab <tab_id>           Close a tab
    navigate <url>               Navigate to URL
    click  --x X --y Y           Click at coordinates
    click  --selector CSS        Click element by CSS selector
    type   <text>                Type text into focused element
    press_enter                  Press Enter key
    key    <key>                 Press a key (Enter, Tab, Escape, ArrowDown, …)
           [--modifiers N]       Modifier bitmask: 1=Alt 2=Ctrl 4=Meta 8=Shift
    scroll [--direction DIR]     Scroll page (up/down/left/right, default: down)
           [--amount N]          Pixels to scroll (default: 500)
    submit_form [--selector CSS] Submit a form
    set_file --selector CSS --files PATH [PATH ...]
    screenshot [--output FILE]   Save screenshot (default: screenshot.png)
    js     <expression>          Evaluate JavaScript and print result
    js_frame <frame> <expr>      Evaluate JS in a specific iframe
    cdp    <method> [params]     Send raw CDP command
    wait   [--strategy STRAT]    Wait for page load (dom/network/both, default: both)
           [--timeout N]         Timeout in seconds (default: 30)
    cookies get [--urls URL ...] Get cookies for URLs
    cookies set <json>           Inject cookies from JSON array
    frames                       List iframes on the page
    alias  set <name> <tab_id>   Set a tab alias
    alias  list                  List tab aliases
    alias  delete <name>         Delete a tab alias
    ddm    [flags ...]           DOM Density Map
    intel  [flags ...]           Page intelligence / extraction strategy
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn

from .chrome import ChromeClient, CDPError
from . import ddm as _ddm
from . import launch as _launch


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


def cmd_launch(args: argparse.Namespace) -> None:
    result = _launch.launch_chrome(
        port=args.port,
        profile=args.profile,
        headless=args.headless,
        startup_url=args.url,
        timeout=args.timeout,
        extra_args=args.chrome_args,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return

    if result["already_running"]:
        print(f"Chrome ready → http://{result['host']}:{result['port']} (already running)")
        print("Profile dir → unknown (attached to existing Chrome on this port)")
    else:
        pid = result.get("pid")
        if pid is None:
            print(f"Chrome started → http://{result['host']}:{result['port']}")
        else:
            print(f"Chrome started → http://{result['host']}:{result['port']} (PID {pid})")
        print(f"Profile dir → {result['profile_dir']}")
    print(f"Startup URL → {result['startup_url']}")


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
    # Inline DDM: navigate always returns page layout so the caller
    # doesn't need a separate DDM call (saves one LLM round-trip).
    # DDM internally runs Intel probe in the same CDP batch.
    try:
        from . import ddm as _ddm_mod
        import io as _io
        old_stdout = sys.stdout
        captured = _io.StringIO()
        sys.stdout = captured
        try:
            _ddm_mod.run_ddm(args.port, tab_id,
                             ["--llm-2pass", "--cols", "60"])
        except SystemExit:
            pass
        finally:
            sys.stdout = old_stdout
        ddm_output = captured.getvalue().strip()
        if ddm_output:
            print(f"\n{ddm_output}")
    except Exception:
        pass  # DDM is best-effort after navigate


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


def cmd_intel(args: argparse.Namespace) -> None:
    from . import intel as _intel
    code = _intel.run_intel(args.port, args.tab, args.intel_flags)
    sys.exit(code)


def cmd_create_tab(client: ChromeClient, args: argparse.Namespace) -> None:
    url = args.url or "about:blank"
    info = client.create_tab(url)
    if args.json:
        print(json.dumps(info, indent=2))
        return
    print(f"Created tab → [{info.get('id', '?')}]  {info.get('url', url)}")


def cmd_close_tab(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.close_tab_id)
    client.close_tab(tab_id)
    print(f"Closed tab {tab_id}")


def cmd_set_file(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    client.set_file(tab_id, args.selector, args.files)
    print(f"Set {len(args.files)} file(s) on {args.selector!r}")


def cmd_press_enter(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    client.key_press(tab_id, "Enter")
    print("Pressed Enter")


def cmd_submit_form(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    result = client.submit_form(tab_id, getattr(args, "selector", None))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    method = result.get("method", "?") if isinstance(result, dict) else "?"
    print(f"Form submitted (via {method})")


def cmd_js_frame(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    result = client.js_eval_frame(tab_id, args.frame_id, args.expression)
    _print_result(result, as_json=args.json)


def cmd_cdp(client: ChromeClient, args: argparse.Namespace) -> None:
    tab_id = client.resolve_tab(args.tab)
    params = {}
    if args.params_json:
        try:
            params = json.loads(args.params_json)
        except json.JSONDecodeError as e:
            _die(f"Invalid JSON params: {e}")
    result = client.send_raw(tab_id, args.method, params)
    print(json.dumps(result, indent=2))


def cmd_status(client: ChromeClient, args: argparse.Namespace) -> None:
    try:
        info = client.browser_version()
    except CDPError:
        print(f"Chrome not running on port {client.port}")
        sys.exit(1)
    if args.json:
        print(json.dumps(info, indent=2))
        return
    print(f"Chrome → {info.get('product', '?')}")
    print(f"Protocol → {info.get('Protocol-Version', '?')}")
    print(f"User-Agent → {info.get('User-Agent', '?')}")


def cmd_kill(args: argparse.Namespace) -> None:
    msg = ChromeClient.kill_chrome(args.port)
    print(msg)


def cmd_alias_set(client: ChromeClient, args: argparse.Namespace) -> None:
    client.set_alias(args.name, args.alias_tab_id)
    print(f"Alias {args.name!r} → {args.alias_tab_id}")


def cmd_alias_list(client: ChromeClient, args: argparse.Namespace) -> None:
    aliases = client.load_aliases()
    if not aliases:
        print("No aliases set.")
        return
    if args.json:
        print(json.dumps(aliases, indent=2))
        return
    for name, tid in aliases.items():
        print(f"  {name} → {tid}")


def cmd_alias_delete(client: ChromeClient, args: argparse.Namespace) -> None:
    client.delete_alias(args.name)
    print(f"Deleted alias {args.name!r}")


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

    # launch
    p = sub.add_parser("launch", help="Launch Chrome with hardened CDP startup")
    p.add_argument("url", nargs="?", default="about:blank",
                   help="Startup URL or page to open if Chrome is already running")
    p.add_argument("--profile", default="default", metavar="NAME",
                   help="Profile name for the dedicated Chrome user-data-dir (default: default)")
    p.add_argument("--headless", action="store_true",
                   help="Launch Chrome headless")
    p.add_argument("--timeout", type=float, default=15.0, metavar="SECS",
                   help="How long to wait for CDP readiness (default: 15)")
    p.add_argument("--chrome-arg", dest="chrome_args", action="append", default=[],
                   metavar="ARG", help="Additional Chrome flag (repeatable)")

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
    p = sub.add_parser("ddm", help="DOM Density Map")
    p.add_argument("ddm_flags", nargs=argparse.REMAINDER,
                   help="Flags passed directly to ddm binary")

    # intel
    p = sub.add_parser("intel", help="Page intelligence / extraction strategy")
    p.add_argument("intel_flags", nargs=argparse.REMAINDER,
                   help="Flags passed directly to intel binary")

    # create_tab
    p = sub.add_parser("create_tab", help="Open a new tab")
    p.add_argument("url", nargs="?", default="about:blank",
                   help="URL to open (default: about:blank)")

    # close_tab
    p = sub.add_parser("close_tab", help="Close a tab")
    p.add_argument("close_tab_id", metavar="TAB_ID",
                   help="Tab ID to close")

    # set_file
    p = sub.add_parser("set_file", help="Set files on a file input element")
    p.add_argument("--selector", required=True, metavar="CSS",
                   help="CSS selector for the file input")
    p.add_argument("--files", nargs="+", required=True, metavar="PATH",
                   help="File path(s) to set")

    # press_enter
    sub.add_parser("press_enter", help="Press Enter key")

    # submit_form
    p = sub.add_parser("submit_form", help="Submit a form")
    p.add_argument("--selector", metavar="CSS",
                   help="CSS selector for form or element inside form")

    # js_frame
    p = sub.add_parser("js_frame", help="Evaluate JS in a specific iframe")
    p.add_argument("frame_id", help="Frame ID or index")
    p.add_argument("expression", help="JS expression to evaluate")

    # cdp (raw)
    p = sub.add_parser("cdp", help="Send raw CDP command")
    p.add_argument("method", help="CDP method (e.g. Page.reload)")
    p.add_argument("params_json", nargs="?",
                   help="Optional JSON params")

    # status
    sub.add_parser("status", help="Check if Chrome is alive")

    # kill
    sub.add_parser("kill", help="Kill Chrome process on this port")

    # agent
    p = sub.add_parser("agent", help="Interactive Claude browser agent")
    p.add_argument("task", nargs="?", help="Initial task (optional)")
    p.add_argument("--model", default="sonnet",
                   help="Model: sonnet, opus, haiku, or full model ID (default: sonnet)")

    # alias
    alias_p = sub.add_parser("alias", help="Tab alias management")
    alias_sub = alias_p.add_subparsers(dest="alias_command", metavar="action")

    ap = alias_sub.add_parser("set", help="Set a tab alias")
    ap.add_argument("name", help="Alias name")
    ap.add_argument("alias_tab_id", metavar="TAB_ID", help="Tab ID")

    alias_sub.add_parser("list", help="List tab aliases")

    ap = alias_sub.add_parser("delete", help="Delete an alias")
    ap.add_argument("name", help="Alias name")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()

    # DDM and Intel pass all flags through to their engines, so we need
    # parse_known_args to avoid argparse eating --text, --probe, etc.
    raw = sys.argv[1:]
    if raw and raw[0] in ("ddm", "intel"):
        # Find the subcommand, parse only global flags before it
        cmd = raw[0]
        # Extract global flags: --port, --tab, --json
        port = int(os.environ.get("UNCHAINED_PORT", 9222))
        tab = "auto"
        i = 1
        passthrough = []
        while i < len(raw):
            if raw[i] == "--port" and i + 1 < len(raw):
                port = int(raw[i + 1])
                i += 2
            elif raw[i] == "--tab" and i + 1 < len(raw):
                tab = raw[i + 1]
                i += 2
            elif raw[i] == "--json":
                i += 1  # ignored for ddm/intel
            else:
                passthrough.append(raw[i])
                i += 1
        args = argparse.Namespace(command=cmd, port=port, tab=tab,
                                  ddm_flags=passthrough if cmd == "ddm" else [],
                                  intel_flags=passthrough if cmd == "intel" else [])
        if cmd == "ddm":
            cmd_ddm(args)
        else:
            cmd_intel(args)
        return

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)
    if args.command == "launch":
        try:
            cmd_launch(args)
            return
        except _launch.LaunchError as e:
            _die(str(e))
    if args.command == "kill":
        cmd_kill(args)
        return
    if args.command == "agent":
        from .agent import run_agent
        run_agent(port=args.port, model=args.model, initial_task=args.task)
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
            case "create_tab":
                cmd_create_tab(client, args)
            case "close_tab":
                cmd_close_tab(client, args)
            case "set_file":
                cmd_set_file(client, args)
            case "press_enter":
                cmd_press_enter(client, args)
            case "submit_form":
                cmd_submit_form(client, args)
            case "js_frame":
                cmd_js_frame(client, args)
            case "cdp":
                cmd_cdp(client, args)
            case "status":
                cmd_status(client, args)
            case "alias":
                ac = getattr(args, "alias_command", None)
                if ac == "set":
                    cmd_alias_set(client, args)
                elif ac == "list":
                    cmd_alias_list(client, args)
                elif ac == "delete":
                    cmd_alias_delete(client, args)
                else:
                    parser.parse_args(["alias", "--help"])
            case _:
                parser.print_help()
                sys.exit(1)

    except CDPError as e:
        _die(str(e))
    except _launch.LaunchError as e:
        _die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
