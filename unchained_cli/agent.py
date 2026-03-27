"""Interactive Claude browser agent — local browsing with DDM-first methodology.

Usage:
    unchained agent                    # Start interactive agent
    unchained agent "search for X"     # Start with an initial task
    unchained agent --model sonnet     # Use a specific model
"""
from __future__ import annotations

import io
import json
import os
import sys
import textwrap
from typing import Any

try:
    import anthropic
except ImportError:
    print(
        "The 'anthropic' package is required for the agent.\n"
        "Install it with: pip install anthropic\n"
        "Or: uv pip install anthropic",
        file=sys.stderr,
    )
    sys.exit(1)

from .chrome import ChromeClient, CDPError

# ---------------------------------------------------------------------------
# System prompt — DDM-first methodology
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""\
You are a browser automation agent. You control a local Chrome browser through
CDP (Chrome DevTools Protocol) tools. Your job is to help the user accomplish
browsing tasks by navigating pages, clicking elements, filling forms, and
extracting information.

## Critical Rules

1. **DDM First, Always** — Use `ddm` for orientation (~500 tokens). Only use
   `screenshot` as last resort for CAPTCHAs or visual-only content (~2100 tokens).

2. **Navigate and Click return DDM inline** — After `navigate` or `click`, the
   page layout is already in the response. Do NOT call `ddm` separately after them.
   Only call `ddm` separately after `type`, or for `--text`, `--at`, `--find`.

3. **Click to focus before typing** — Always click the target input first.
   Key events go to whichever element has focus.

4. **Probe on first visit** — On the first page of a new domain, the Intel probe
   runs automatically with DDM. Check the strategy line to decide extraction method:
   - `js_global > 50%` → use `intel --stores` → `intel --find-paths` → `js`
   - `host_attrs > 50%` → use `intel --extract --strategy host_attrs`
   - `data_testid > 40%` → use `intel --extract --strategy data_testid`
   - Otherwise → stick with DDM (`--text`, `--at`, JS `querySelectorAll`)

## Workflow

1. **ORIENT** — Read the DDM layout from navigate/click output
2. **IDENTIFY** — `ddm --at x,y` on elements you need details about
3. **ACT** — Click coordinates, type text, or run JS
4. **VERIFY** — Check the layout changed after actions
5. **EXTRACT** — Use `ddm --text`, `intel --extract`, or `js` based on page type

## Tool Tips

- Coordinates come from DDM: `label@x,y` format. Use those x,y for clicks.
- `ddm --text --find "keyword"` searches page text and shows nearby elements.
- `ddm --tabs` lists open tabs. Use `--tab <id>` on any tool for multi-tab work.
- `js` can run arbitrary JavaScript for complex extraction or interaction.
- `intel --stores` reveals JS data stores (YouTube, Next.js, Nuxt, etc.)
""")

# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate to a URL. Returns page layout with interactive elements and Intel probe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "tab": {"type": "string", "description": "Tab ID (optional, default: current tab)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click at pixel coordinates or a CSS selector. Returns updated page layout.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X pixel coordinate"},
                "y": {"type": "integer", "description": "Y pixel coordinate"},
                "selector": {"type": "string", "description": "CSS selector (alternative to x,y)"},
            },
        },
    },
    {
        "name": "type_text",
        "description": "Type text into the currently focused element. Click the input first!",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "press_enter",
        "description": "Press Enter key (submit form, confirm input, etc.)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "key_press",
        "description": "Press a keyboard key (Escape, Tab, ArrowDown, etc.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name"},
                "modifiers": {"type": "integer", "description": "Modifier bitmask: 1=Alt 2=Ctrl 4=Meta 8=Shift", "default": 0},
            },
            "required": ["key"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page in a direction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
                "amount": {"type": "integer", "description": "Pixels to scroll (default: 500)", "default": 500},
            },
        },
    },
    {
        "name": "ddm",
        "description": "DOM Density Map — text-based page layout for orientation. Use flags: --text (extract text), --find <keyword> (search text), --at <x>,<y> (element details at coords), --tabs (list tabs), --sparse (compressed), --interactive (elements only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "DDM flags, e.g. ['--text', '--find', 'price'] or ['--at', '694,584']",
                    "default": [],
                },
            },
        },
    },
    {
        "name": "intel",
        "description": "Page intelligence — fingerprint page and rank extraction strategies. Use flags: --probe (fingerprint), --extract (full pipeline), --stores (JS data stores), --shape <global> (variable structure), --find-paths <global> <pattern> (search globals).",
        "input_schema": {
            "type": "object",
            "properties": {
                "flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Intel flags, e.g. ['--probe'] or ['--extract', '--strategy', 'host_attrs']",
                    "default": [],
                },
            },
        },
    },
    {
        "name": "js",
        "description": "Execute JavaScript on the page and return the result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "JavaScript expression to evaluate"},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot (last resort — costs ~2100 tokens). Use DDM first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output": {"type": "string", "description": "Output file path", "default": "/tmp/unchained_screenshot.png"},
            },
        },
    },
    {
        "name": "create_tab",
        "description": "Open a new browser tab, optionally navigating to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open", "default": "about:blank"},
            },
        },
    },
    {
        "name": "close_tab",
        "description": "Close a browser tab by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "Tab ID to close"},
            },
            "required": ["tab_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _capture_ddm(port: int, tab_id: str, flags: list[str]) -> str:
    """Run DDM and capture stdout."""
    from . import ddm as _ddm
    old_stdout, old_stderr = sys.stdout, sys.stderr
    captured = io.StringIO()
    err = io.StringIO()
    sys.stdout = captured
    sys.stderr = err
    try:
        _ddm.run_ddm(port, tab_id, flags)
    except SystemExit:
        pass
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    output = captured.getvalue()
    errors = err.getvalue()
    if errors and not output:
        return f"Error: {errors.strip()}"
    return output.strip()


def _capture_intel(port: int, tab_id: str, flags: list[str]) -> str:
    """Run Intel and capture stdout."""
    from . import intel as _intel
    old_stdout, old_stderr = sys.stdout, sys.stderr
    captured = io.StringIO()
    err = io.StringIO()
    sys.stdout = captured
    sys.stderr = err
    try:
        _intel.run_intel(port, tab_id, flags)
    except SystemExit:
        pass
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    output = captured.getvalue()
    errors = err.getvalue()
    if errors and not output:
        return f"Error: {errors.strip()}"
    return output.strip()


def execute_tool(client: ChromeClient, tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result string."""
    port = client.port
    tab = tool_input.get("tab", "auto")

    try:
        if tool_name == "navigate":
            tab_id = client.resolve_tab(tab)
            client.navigate(tab_id, tool_input["url"])
            final = client.js_eval(tab_id, "window.location.href") or tool_input["url"]
            result = f"Navigated → {final}\n"
            # Inline DDM + Intel probe
            ddm_out = _capture_ddm(port, tab_id, ["--llm-2pass", "--cols", "60"])
            if ddm_out:
                result += f"\n{ddm_out}"
            return result

        elif tool_name == "click":
            tab_id = client.resolve_tab(tab)
            if "selector" in tool_input and tool_input["selector"]:
                pos = client.click_selector(tab_id, tool_input["selector"])
                click_desc = f"Clicked {tool_input['selector']!r} at ({int(pos['x'])}, {int(pos['y'])})"
            else:
                x, y = tool_input.get("x", 0), tool_input.get("y", 0)
                client.click(tab_id, x, y)
                click_desc = f"Clicked ({x}, {y})"
            # Inline DDM after click (no probe — same domain)
            ddm_out = _capture_ddm(port, tab_id, ["--llm-2pass", "--cols", "60", "--no-probe"])
            result = click_desc
            if ddm_out:
                result += f"\n\n{ddm_out}"
            return result

        elif tool_name == "type_text":
            tab_id = client.resolve_tab(tab)
            client.type_text(tab_id, tool_input["text"])
            preview = tool_input["text"][:40]
            return f"Typed: {preview!r}"

        elif tool_name == "press_enter":
            tab_id = client.resolve_tab(tab)
            client.key_press(tab_id, "Enter")
            # Inline DDM after enter (may submit form / navigate)
            ddm_out = _capture_ddm(port, tab_id, ["--llm-2pass", "--cols", "60", "--no-probe"])
            result = "Pressed Enter"
            if ddm_out:
                result += f"\n\n{ddm_out}"
            return result

        elif tool_name == "key_press":
            tab_id = client.resolve_tab(tab)
            client.key_press(tab_id, tool_input["key"], tool_input.get("modifiers", 0))
            return f"Pressed {tool_input['key']}"

        elif tool_name == "scroll":
            tab_id = client.resolve_tab(tab)
            direction = tool_input.get("direction", "down")
            amount = tool_input.get("amount", 500)
            client.scroll(tab_id, direction, amount)
            # DDM after scroll to show new viewport
            ddm_out = _capture_ddm(port, tab_id, ["--llm-2pass", "--cols", "60", "--no-probe"])
            result = f"Scrolled {direction} {amount}px"
            if ddm_out:
                result += f"\n\n{ddm_out}"
            return result

        elif tool_name == "ddm":
            tab_id = client.resolve_tab(tab)
            flags = tool_input.get("flags", [])
            return _capture_ddm(port, tab_id, flags)

        elif tool_name == "intel":
            tab_id = client.resolve_tab(tab)
            flags = tool_input.get("flags", ["--probe"])
            return _capture_intel(port, tab_id, flags)

        elif tool_name == "js":
            tab_id = client.resolve_tab(tab)
            result = client.js_eval(tab_id, tool_input["expression"])
            if isinstance(result, (dict, list)):
                return json.dumps(result, indent=2)
            return str(result) if result is not None else "(undefined)"

        elif tool_name == "screenshot":
            tab_id = client.resolve_tab(tab)
            png = client.screenshot(tab_id)
            output = tool_input.get("output", "/tmp/unchained_screenshot.png")
            with open(output, "wb") as f:
                f.write(png)
            return f"Screenshot saved → {output} ({len(png):,} bytes)"

        elif tool_name == "create_tab":
            url = tool_input.get("url", "about:blank")
            info = client.create_tab(url)
            return f"Created tab [{info.get('id', '?')}] → {info.get('url', url)}"

        elif tool_name == "close_tab":
            client.close_tab(tool_input["tab_id"])
            return f"Closed tab {tool_input['tab_id']}"

        else:
            return f"Unknown tool: {tool_name}"

    except CDPError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}


def run_agent(port: int = 9222, model: str = "sonnet", initial_task: str | None = None):
    """Run the interactive Claude browser agent."""
    model_id = MODEL_ALIASES.get(model, model)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        print("Set it with: export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        sys.exit(1)

    sdk = anthropic.Anthropic(api_key=api_key)
    client = ChromeClient(port=port)

    # Verify Chrome is reachable
    try:
        client.browser_version()
    except CDPError:
        print(f"Chrome not reachable on port {port}.")
        print(f"Start it with: unchained launch")
        sys.exit(1)

    messages: list[dict] = []

    print(f"Unchained Agent — model: {model_id}, port: {port}")
    print("Type your task, or 'quit' to exit.\n")

    # If initial task provided, use it as first message
    if initial_task:
        print(f"You: {initial_task}\n")
        messages.append({"role": "user", "content": initial_task})
    else:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            return
        messages.append({"role": "user", "content": user_input})

    while True:
        # Call Claude
        try:
            response = sdk.messages.create(
                model=model_id,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:
            print(f"\nAPI Error: {e}")
            break

        # Process response
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # Print text blocks and collect tool uses
        tool_uses = []
        for block in assistant_content:
            if block.type == "text":
                print(f"\nAgent: {block.text}")
            elif block.type == "tool_use":
                tool_uses.append(block)

        # If no tool calls, wait for next user input
        if response.stop_reason == "end_turn" or not tool_uses:
            print()
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not user_input or user_input.lower() in ("quit", "exit", "q"):
                return
            messages.append({"role": "user", "content": user_input})
            continue

        # Execute tool calls
        tool_results = []
        for tool_use in tool_uses:
            name = tool_use.name
            inp = tool_use.input
            print(f"\n  [{name}] {json.dumps(inp, separators=(',', ':'))[:120]}")

            result = execute_tool(client, name, inp)

            # Truncate very long results
            if len(result) > 8000:
                result = result[:7900] + f"\n... (truncated, {len(result)} chars total)"

            # Show brief preview
            preview = result[:200].replace('\n', ' ')
            if len(result) > 200:
                preview += "..."
            print(f"  → {preview}")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})
