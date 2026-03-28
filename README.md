# unchainedsky-cli

Browser automation CLI over local Chrome CDP — DDM-first methodology for LLM agents.

## Install

```bash
# With uv (recommended)
uv tool install unchainedsky-cli[agent]

# Or pip
pip install unchainedsky-cli[agent]

# Or brew
brew install --HEAD unchainedsky/tap/unchainedsky-cli
```

The `[agent]` extra includes the Anthropic SDK for the interactive Claude agent. Omit it if you only need the CLI tools.

## Quick Start

```bash
# Launch Chrome and browse
unchained launch https://news.ycombinator.com

# Navigate returns page layout + intel probe automatically
unchained navigate https://example.com

# Search page text
unchained ddm --text --find "price"

# Element details at coordinates
unchained ddm --at 694,584

# Start the interactive Claude agent
export ANTHROPIC_API_KEY=sk-ant-...
unchained agent "find the cheapest flight to NYC"
```

## Launch Options

```bash
# Sandboxed profile (default — clean, isolated)
unchained launch

# Existing Chrome profile (with cookies, logins, extensions)
unchained --port 9333 launch --use-profile --profile "Profile 3" https://x.com

# Headless with stealth (auto-enables fingerprint evasion)
unchained launch --headless https://example.com

# Stealth mode (visible window, bot detection evasion)
unchained launch --stealth https://example.com

# Multiple profiles on different ports
unchained --port 9222 launch --profile work
unchained --port 9333 launch --use-profile --profile "Profile 3"
unchained --port 9444 launch --use-profile --profile "Profile 8"
```

## Commands

### Browser Lifecycle

| Command | Description |
|---------|-------------|
| `launch [url]` | Launch Chrome with CDP (`--profile`, `--use-profile`, `--stealth`, `--headless`) |
| `status` | Check if Chrome is alive |
| `kill` | Kill Chrome on this port |

### Navigation & Interaction

| Command | Description |
|---------|-------------|
| `navigate <url>` | Navigate (returns inline DDM layout + Intel probe) |
| `click --x X --y Y` | Click at pixel coordinates from DDM |
| `click --selector CSS` | Click by CSS selector |
| `type <text>` | Type into focused element |
| `press_enter` | Press Enter key |
| `key <key> [--modifiers N]` | Press any key (Escape, Tab, ArrowDown, etc.) |
| `scroll [--direction DIR]` | Scroll page (up/down/left/right, default: 500px) |
| `submit_form [--selector]` | Submit a form |
| `set_file --selector CSS --files PATH...` | Set files on file input |

### Page Intelligence

| Command | Description |
|---------|-------------|
| `ddm` | DOM Density Map — text layout with interactive elements |
| `ddm --text` | Extract page text (innerText) |
| `ddm --text --find "keyword"` | Search page text, show nearby elements |
| `ddm --at 694,584` | Element details at pixel coordinates |
| `ddm --sparse` | RLE-compressed output (~100-400 tokens) |
| `ddm --interactive` | Interactive elements only (smallest output) |
| `ddm --llm-2pass` | LLM-optimized layout (default for navigate) |
| `ddm --tabs` | List open tabs |
| `ddm --forms` | Detect forms as callable contracts |
| `intel --probe` | Fingerprint page + rank 8 extraction strategies |
| `intel --extract` | Full extraction pipeline (auto-selects strategy) |
| `intel --stores` | List JS data stores (YouTube, Next.js, Nuxt, etc.) |
| `intel --shape <global>` | Map JS global variable structure |
| `intel --find-paths <global> <pattern>` | Search paths in JS globals |

### Data & Tabs

| Command | Description |
|---------|-------------|
| `js <expression>` | Evaluate JavaScript |
| `js_frame <frame> <expr>` | Evaluate JS in a specific iframe |
| `screenshot [--output FILE]` | Save screenshot (last resort — use DDM first) |
| `tabs` | List open tabs |
| `create_tab [url]` | Open a new tab |
| `close_tab <tab_id>` | Close a tab |
| `cookies get [--urls URL...]` | Get cookies |
| `cookies set <json>` | Inject cookies |
| `frames` | List iframes |
| `cdp <method> [params]` | Send raw CDP command |
| `wait [--strategy dom\|network\|both]` | Wait for page load |
| `alias set <name> <tab_id>` | Set a tab alias |
| `alias list` | List tab aliases |

### Agent

| Command | Description |
|---------|-------------|
| `agent [task]` | Interactive Claude browser agent |
| `agent --model opus "task"` | Use a specific model (sonnet/opus/haiku) |

## DDM-First Methodology

Every browsing task follows this pipeline:

1. **ORIENT** — `navigate` and `click` return DDM layout inline. Read it. Don't call `ddm` separately.
2. **IDENTIFY** — `ddm --at x,y` on elements you need details about.
3. **CLASSIFY** — Check the Intel probe in navigate output. If `js_global > 50%`, use `intel --stores`. If `host_attrs > 50%`, use `intel --extract`.
4. **ACT** — Click coordinates from DDM, type text, or run JS.
5. **VERIFY** — Check the layout changed after actions.
6. **EXTRACT** — Use `ddm --text`, `intel --extract`, or `js` based on page type.

### Token Comparison

| Method | Tokens | Info |
|--------|--------|------|
| Screenshot (base64 PNG) | ~2,100 | Visual only, no selectors |
| `ddm --llm-2pass` | ~500 | Layout + interactive elements |
| `ddm --sparse` | ~100-400 | RLE-compressed, varies by complexity |
| `intel --probe` | ~50 | Fingerprint + strategy ranking |

## Global Options

```
--port PORT    Chrome remote debugging port (default: 9222, env: UNCHAINED_PORT)
--tab TAB_ID   Target tab ID, alias, or 'auto' (default: auto)
--json         Output raw JSON
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UNCHAINED_PORT` | `9222` | Chrome remote debugging port |
| `UNCHAINED_DATA_DIR` | `~/.unchained` | Base directory for Chrome profiles |
| `UNCHAINED_CHROME_BIN` | — | Chrome/Chromium binary override |
| `UNCHAINED_DDM_BIN` | — | DDM binary path override |
| `UNCHAINED_INTEL_BIN` | — | Intel binary path override |
| `ANTHROPIC_API_KEY` | — | Required for `agent` command |

## Build Binaries

DDM and Intel can be compiled to native binaries (protects proprietary algorithms):

```bash
pip install nuitka ordered-set
python build_binaries.py --install
```

Binaries are installed to `~/.unchained/bin/`. The CLI automatically uses them when available, falling back to the Python engine otherwise.

## Requirements

- Python 3.10+
- Google Chrome, Chromium, or Microsoft Edge
