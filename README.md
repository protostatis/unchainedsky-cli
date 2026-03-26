# unchainedsky-cli

Browser automation CLI over local Chrome CDP. No relay, no auth — pure local dev tool.

## Install

```bash
brew install --HEAD unchainedsky/tap/unchainedsky-cli
```

Or with pip:

```bash
python3.10 -m pip install unchainedsky-cli
```

## Requirements

Chrome must be running with remote debugging enabled:

```bash
open -a "Google Chrome" --args --remote-debugging-port=9222
```

Python 3.10+ is required.

## Usage

```
unchained [--port PORT] [--tab TAB_ID] <command> [args]
```

### Commands

| Command | Description |
|---------|-------------|
| `tabs` | List open tabs |
| `navigate <url>` | Navigate to URL |
| `click --x X --y Y` | Click at coordinates |
| `click --selector CSS` | Click element by CSS selector |
| `type <text>` | Type into focused element |
| `scroll [--direction DIR] [--amount N]` | Scroll (up/down/left/right) |
| `screenshot [--output FILE]` | Save screenshot (default: screenshot.png) |
| `js <expression>` | Evaluate JavaScript |
| `key <key> [--modifiers N]` | Press a key |
| `wait [--strategy dom\|network\|both]` | Wait for page load |
| `cookies get [--urls URL ...]` | Get cookies |
| `cookies set <json>` | Inject cookies |
| `frames` | List iframes |
| `ddm [flags ...]` | DOM Density Map (requires ddm binary) |

### Examples

```bash
# Navigate and interact
unchained navigate https://example.com
unchained click --selector "button.submit"
unchained type "hello world"
unchained key Enter

# Target a specific tab or port (`--tab auto` uses the first page tab)
unchained --port 9223 --tab <tab-id> navigate https://example.com

# Extract data
unchained js "document.title"
unchained js "[...document.querySelectorAll('h2')].map(e => e.textContent)"

# Screenshot
unchained screenshot --output page.png

# Cookies
unchained cookies get
unchained cookies get --urls https://example.com https://api.example.com
unchained cookies set '[{"name":"session","value":"abc","domain":".example.com"}]'
```

### DDM

The `ddm` command requires a separately distributed binary:

```bash
brew install unchainedsky/tap/unchainedsky-ddm
```

Or set `UNCHAINED_DDM_BIN=/path/to/ddm`.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UNCHAINED_PORT` | `9222` | Chrome remote debugging port |
| `UNCHAINED_DDM_BIN` | — | Path to ddm binary override |
