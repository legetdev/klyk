# Klyk

[![PyPI](https://img.shields.io/pypi/v/klyk)](https://pypi.org/project/klyk/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/klyk/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

> ## ⚠️ What klyk is — and what it isn't
>
> **Read this before you install.**
>
> klyk gives an AI agent real, OS-level control of your Mac — the same input a human has. It moves the cursor, fires keystrokes, and clicks what's on screen. In its default mode it does this **invisibly and autonomously**: no cursor movement you can watch, no confirmation prompts — acting on its own once the agent decides to.
>
> **This is powerful, and it is dangerous.** Be clear-eyed about what that means:
>
> - **It can take real, irreversible actions.** A click is a click. klyk can press *Buy*, *Send*, *Confirm Transfer*, *Delete*, or *Sign* just as you could. The only guardrails are a window-bounds check and a text instruction asking the agent to confirm first — an instruction the agent *can ignore*. There is no sandbox and no spending limit.
> - **It runs with your full user privileges.** Anything you can do on your Mac, klyk can do. It does not isolate itself or drop privileges.
> - **It is a prompt-injection target.** If the agent driving klyk also reads untrusted content — a web page, an email, a document — a malicious instruction hidden there can become real clicks and keystrokes on your machine. "Reads the web" + "controls the Mac" is the high-risk combination. Run klyk only with an agent and a workflow you trust.
> - **It relies on an undocumented Apple API.** Invisible input uses Apple's private SkyLight framework. Apple does not support or guarantee it; a macOS update can change or break it without notice (klyk falls back to a visible cursor when it can).
>
> **What klyk is, honestly:** an early, experimental, open-source tool built by a solo author — a business student, not a professional developer — working with AI, in good faith. It has **not** had an independent professional security audit. Core paths are live-tested end-to-end against a real macOS session before every release, but treat it as early software: less-common paths may hold surprises.
>
> **No warranty. Use at your own risk.** klyk is provided "as is" under the MIT license, with no warranty of any kind. You are responsible for what the agent does on your machine. Don't point it at anything — money, accounts, irreplaceable files — you aren't willing to have an autonomous agent touch.

**OS-level macOS app testing for AI agents.** Click real buttons, type real keys, see what actually rendered. Native apps, Electron apps, browsers, web pages, system dialogs — anything visible on the screen.

> **Status:** Portfolio project. Showcases product thinking and shipped tooling. Bug reports won't be actively triaged. Well-scoped PRs are welcome — but expect a slow review cadence.

---

## The problem

AI assistants are increasingly asked to test, validate, or operate desktop apps end-to-end. Today they can't. Existing automation tools either require deep app instrumentation (XCUITest, Appium) or simulate user input at a layer too brittle to be trusted (pixel-only click frameworks, headless DOM scrapers). The result: agents that can write apps faster than ever, but can't verify they actually work.

Klyk closes that gap. It gives an AI agent the same input channel a human has — real cursor moves via Apple's CoreGraphics API, real keystrokes posted to the HID event tap, real composited screenshots — and a clean MCP interface to drive it. The agent observes, decides, acts, verifies. The way a human would.

## What it does

```
> screenshot the app, then click "Sign in"
[ Klyk takes a real screenshot via CoreGraphics, returns it + the AX tree ]
[ click_element finds "Sign in" via accessibility, falls back to OCR, then to template match ]
[ Real click fires through the HID event tap — indistinguishable from a human pressing the button ]
```

A flat, MECE tool surface across observation, interaction, evaluation, session management, and system operations. Three-tier click targeting (AX → on-device OCR → pixel template) so a label is reachable regardless of how the app exposes it. Cross-app drag, right-click-then-select, and multilingual OCR are all first-class. Per-call latency + reasoning-gap metrics so the agent can self-pace. Best-effort AX folded into screenshots so most tasks finish in one round-trip.

## Install

```bash
pipx install klyk        # isolated install — recommended
klyk install
```

> **Use `pipx` (or `uv tool install klyk`), not bare `pip`.** Klyk pulls a modern NumPy; installing it into your global Python can clash with other packages pinned to older versions. `pipx`/`uv` give klyk its own environment while still putting `klyk` and `klyk-call` on your PATH — same commands, zero blast radius. Plain `pip install klyk` works only if you want it in the current environment and accept that risk.

`klyk install` is a turnkey first-run flow:

1. Adds Klyk to `~/.claude.json` (so it appears in every Claude Code session).
2. Walks you through granting the two macOS permissions Klyk needs — opens the exact System Settings panes for **Accessibility** and **Screen Recording**, waits for you to add your terminal app, then **verifies the grant actually came through** before continuing.
3. Runs a final `klyk doctor` pass to confirm every piece is green.
4. Detects every *other* AI client on your Mac and offers to wire them all in one confirmation — permissions carry over, so it's a single prompt, not a setup pass per client.

For clients that read a natural-language context file (Gemini CLI's `GEMINI.md`), `install` also *offers* — opt-in, defaults to no — to add a short, clearly-marked klyk note there, so the agent can fall back to the `klyk-call` shell if its own MCP ever fails to surface klyk. It never edits that file without an explicit yes, merges around your existing content, and `uninstall` removes it.

Restart Claude Code (or whichever MCP client you use) and klyk is live. Try `inspect Finder` to see it in action. To wire every detected client up front in one shot: `klyk install --all`.

**Troubleshooting: `klyk doctor`.** Run it any time something's off. Reports every dependency, permission, and config grant klyk needs as ✓ / ⚠ / ✗ with the exact next step on anything that's not green. `--json` gives a structured payload for tooling.

### Staying up to date

```bash
klyk update
```

One command, always correct: it detects **how** klyk was installed (pipx, `uv tool`, or plain pip), runs the matching upgrade, and then **restarts the running klyk server automatically** — every connected AI client loads the new version on its next tool call, no client restarts, no config edits. Since all your clients point at the same klyk install, one update covers every agent at once.

You never have to wonder whether you're behind, either:

- **`klyk doctor`** includes a `klyk version` line — `0.2.0 (latest release)` or `0.2.0 → 0.3.0 available` with the exact command to run.
- **The menu-bar eye** shows a one-line `⬆ Update available` notice when a newer release exists.
- **`klyk update --check`** reports whether an update exists without changing anything.

Behind these sits a single once-a-day check of klyk's own PyPI metadata (nothing about you or your screen is sent — see the Security model below), cached in `~/.klyk/update_check.json` and fully offline-safe. Set `KLYK_UPDATE_CHECK=0` to disable it entirely.

**One driver at a time.** Any number of MCP clients can have klyk configured and connected simultaneously (Claude Code *and* Cursor *and* Gemini CLI…) — each spawns its own klyk process, and none is ever refused. But only **one** session holds the control token and actually drives the Mac at a time, so two agents can never interleave clicks and keystrokes into the same app. A new session takes control automatically when the previous driver is gone; taking over from a *live* driver is an explicit `take_control` call. Exactly one menu-bar eye is visible: the active driver's.

### Use with other MCP clients

`klyk install <client>` auto-configures any supported client — same turnkey flow as Claude (writes the config, grants permissions, runs a health check):

```bash
klyk install cursor      # or: windsurf · continue · cline · codex · gemini · antigravity (agy) · grok
klyk install --list      # show every supported client and its config path
```

| Client | Config file |
|---|---|
| Claude Code | `~/.claude.json` (the default: `klyk install`) |
| Cursor | `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Continue | `~/.continue/config.json` |
| Cline | VS Code globalStorage `cline_mcp_settings.json` |
| OpenAI Codex CLI | `~/.codex/config.toml` |
| Gemini CLI | `~/.gemini/settings.json` |
| Antigravity CLI (`agy`) | `~/.gemini/antigravity-cli/mcp_config.json` |
| Grok CLI (xAI) | `~/.grok/config.toml` |

Any other MCP client works too — klyk speaks MCP natively. Add this entry to its config wherever it lives — use the **full path to the Python klyk is installed in** as `command` (run `python -c "import sys;print(sys.executable)"` in that env; `klyk install` fills this in automatically). A bare `python3` only works if klyk is in your global Python:

```json
{ "mcpServers": { "klyk": { "command": "/path/to/python", "args": ["-m", "klyk.mcp_server"] } } }
```

Permissions, the singleton lock, and `klyk doctor` work identically regardless of which client launches klyk.

### Choosing a model — the speed vs. intelligence trade-off

klyk runs at the same OS speed no matter what's driving it — the latency and accuracy you *feel* are the **model's**, not klyk's. Because klyk is model-agnostic, you pick where you want to sit on a very real trade-off:

- **Frontier models (e.g. Claude Opus).** Long gaps between actions — sometimes **minutes** while the model reasons — but each action is usually well-chosen, correctly targeted, and efficient. Fewer wrong clicks, fewer wasted round-trips. Best for high-stakes or irreversible work where a misfire is expensive.
- **Fast, smaller models (e.g. Gemini Flash).** Often **under ~10 seconds** between actions — snappy and cheap — but they misfire more: wrong element, wrong order, premature verdict. They lean harder on klyk's observe→act→verify loop to catch and recover from mistakes. Best for fast, iterative, low-stakes tasks where throughput beats precision.

There's no universally "right" model — it's a deliberate choice per task. Match the model to the cost of being wrong: the smarter-but-slower model when an errant click matters, the faster-but-looser model when speed and volume matter and recovery is cheap.

### Drive klyk from any AI (no MCP integration required)

MCP support varies a lot between agent harnesses — some gate it, some implement it incompletely, some don't have it. So klyk ships an extra front door that works even when a client's own MCP plumbing doesn't. It reaches the **same** persistent klyk session.

**`klyk-call` — one shell command, any tool.** For any agent that can run a shell command (great for smaller models — no handshake to reason about):

```bash
klyk-call --list                              # all tools + their parameter names
klyk-call --schema inspect                    # full JSON schema for one tool
klyk-call --tool inspect --app Finder         # call any tool
klyk-call --tool screenshot --app Finder      # screenshot → saved to disk, path returned
echo '{"tool":"screen_info","args":{}}' | klyk-call --batch   # many calls, one session
```

**Vision over the shell.** When a tool returns a screenshot (`screenshot`, `inspect`, `verdict`, image-producing `run` steps), `klyk-call` writes the PNG to `~/.klyk/captures/` and returns its `saved_path` instead of dumping base64 — so the agent *views* the capture with its own image reader (Claude Code's file read, Gemini's `@path`, etc.) and keeps the same observe→act→verify loop the native MCP transport has, with no context-flooding payload. The cache keeps the most recent 20 captures.

## Quick example

```
screenshot(app="Google Chrome")
# → returns the image plus the AX element list

run(app="Google Chrome", actions=[
    {"tool": "click", "x": 580, "y": 389},
    {"tool": "fill_field", "x": 580, "y": 389, "text": "This video is incredible!"},
    {"tool": "screenshot"}
])
# → executes the full sequence at OS speed, returns one batched response

verdict(app="Google Chrome", test_description="Posted a comment on a YouTube video")
# → returns final screenshot + logs + grading criteria for the agent to synthesize PASS/FAIL
```

## Why these specific trade-offs

The interesting product decisions weren't tools to build but tools to *not* build:

- **No third-party computer-use libraries.** Everything runs through Apple's CoreGraphics, Vision, and Accessibility frameworks via Python's `ctypes`. Keeps the dependency footprint tiny and the failure surface predictable.
- **No visual grounding models.** UI-TARS and OmniParser would have closed the "no AX, no text" gap — at the cost of a multi-GB model download and 1–2s per call. Rejected. Template matching covers the same case at 30ms with zero ML dependencies.
- **No Chrome DevTools Protocol.** It would have helped only Chromium browsers and broken the "like a human" model. Skipped in favor of forcing the renderer-accessibility flag, which gets the entire web AX tree for free.
- **macOS only.** Cross-platform compromises every primitive. The honest framing: ship one OS well rather than three OSes badly.

Every tool is designed against the same set of failure modes — ambiguity, accidental retries, token bloat, lost reactivity from batching, and so on. The agent-facing contract for each tool lives in its `description` field in `klyk/mcp_server.py`.

## What's inside

| Module | Purpose |
|---|---|
| `mcp_server.py` | MCP server, tool definitions, dispatch |
| `session.py` | Per-app session registry, auto-launch, template cache |
| `computer.py` | CoreGraphics input synthesis (click, drag, keyboard, scroll, AX) |
| `capture.py` | CoreGraphics screenshot capture (in-memory primary, screencapture fallback) |
| `launcher.py` | App launch with browser-aware AX flag injection |
| `ocr.py` | Apple Vision OCR (two-pass: fast then accurate) |
| `matcher.py` | Pure-NumPy template matching (FFT + integral-image NCC) with template cache support |
| `grader.py`, `reporter.py` | Verdict + UI grading helpers |
| `updates.py` | Update awareness (daily cached PyPI check) + `klyk update` plumbing |
| `keycodes.py`, `logs.py` | Low-level support |

For the full tool reference and behavior contracts, see the tool `description` fields in `klyk/mcp_server.py`. For how the internals are shaped and why, see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Security model & trust scope

Klyk is a thin pipe between the agent and the OS. Its trust model is straightforward:

- **Local only.** Your screen contents and inputs stay on your machine — screenshots, OCR results, AX labels, and tool responses never leave it via klyk. klyk's **only** network call is an optional once-daily version check against PyPI's public metadata for the `klyk` package (a plain HTTPS GET — it sends nothing about you, your screen, or your usage). Disable it with `KLYK_UPDATE_CHECK=0` and klyk makes no network calls at all.
- **macOS permissions are the consent surface.** Accessibility and Screen Recording must be granted explicitly via System Settings; `klyk doctor` shows the current state.
- **The agent controls every action.** Klyk doesn't decide what to click or type — it executes what the agent asks. Run klyk only with agents you trust to act on your behalf.
- **Stderr from launched apps is captured for the `verdict` payload.** klyk attempts to scrub common credential patterns (passwords, API keys, JWTs, AWS keys, bearer tokens) on a **best-effort** basis — it cannot catch every format, so do not rely on it as your only safeguard.
- **Private framework usage.** Klyk uses Apple's private SkyLight framework for invisible input delivery (the "autonomous" mode). This is fine for CLI / PyPI distribution but is the reason klyk can't ship via the Mac App Store.
- **Reporting a vulnerability.** See [`SECURITY.md`](./SECURITY.md).

## License

MIT — see [`LICENSE`](./LICENSE).

## Disclaimer — No Warranty

klyk is provided **"AS IS", without warranty of any kind**, express or implied, including merchantability, fitness for a particular purpose, and non-infringement (see [`LICENSE`](./LICENSE)). To the maximum extent permitted by law, the author is **not liable** for any damage, data loss, financial loss, account action, privacy exposure, or other harm arising from the use, misuse, or malfunction of klyk — whether caused by the software, the AI agent driving it, or a third-party dependency or macOS framework it relies on. Its safety measures (credential scrubbing, bounds checks, the emergency-stop chord, confirm-destructive flags) are **best-effort and agent-cooperative only** — not a guarantee, and not to be relied upon as your sole safeguard. **You run klyk at your own risk and are solely responsible for what you connect it to and what it does.**

---

*Designed and shipped using AI as implementation partner. The product decisions, scope choices, and trade-offs are mine.*
