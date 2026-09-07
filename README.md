# Klyk

[![PyPI](https://img.shields.io/pypi/v/klyk)](https://pypi.org/project/klyk/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/klyk/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

> ## ⚠️ What klyk is — and what it isn't
>
> **Read this before you install.**
>
> klyk gives an AI agent real, OS-level control of your Mac — the same input a human has. It moves the cursor, fires keystrokes, and clicks what's on screen. In its default mode it **attempts invisible native input** and acts autonomously once the agent decides to. Chromium interactions, command shortcuts and paste, menus and dialogs, and some long presses may activate the app or use visible input; klyk does not guarantee that every native action stays invisible.
>
> **This is powerful, and it is dangerous.** Be clear-eyed about what that means:
>
> - **It can take real, irreversible actions.** A click is a click. klyk can press *Buy*, *Send*, *Confirm Transfer*, *Delete*, or *Sign* just as you could. Klyk enforces checks such as target-window bounds, ownership, duplicate-label rejection, and its emergency latch, while confirmation guidance and a bounds override remain agent-cooperative and do not establish user approval. There is no sandbox and no spending limit.
> - **It runs with your full user privileges.** Anything you can do on your Mac, klyk can do. It does not isolate itself or drop privileges.
> - **It is a prompt-injection target.** If the agent driving klyk also reads untrusted content — a web page, an email, a document — a malicious instruction hidden there can become real clicks and keystrokes on your machine. "Reads the web" + "controls the Mac" is the high-risk combination. Run klyk only with an agent and a workflow you trust.
> - **It relies on an undocumented Apple API.** Invisible native input uses Apple's private SkyLight framework. Apple does not support or guarantee it; a macOS update can change or break it without notice, and the affected actions may fall back to visible input or require activation.
>
> **What klyk is, honestly:** an early, experimental, open-source tool built by a solo author — a business student, not a professional developer — working with AI, in good faith. It has **not** had an independent professional security audit. Core paths have repeatable live macOS tests; releases use full or targeted verification according to the changed behavior (see [the release procedure](./tests/README.md#release-gate)). Treat it as early software: less-common paths may hold surprises.
>
> **No warranty. Use at your own risk.** klyk is provided "as is" under the MIT license, with no warranty of any kind. You are responsible for what the agent does on your machine. Don't point it at anything — money, accounts, irreplaceable files — you aren't willing to have an autonomous agent touch.

**OS-level macOS app testing for AI agents.** Click real buttons, type real keys, see what actually rendered. Native apps, Electron apps, browsers, web pages, system dialogs — anything visible on the screen.

> **Status:** Portfolio project. Showcases product thinking and shipped tooling. Bug reports won't be actively triaged. Well-scoped PRs are welcome — but expect a slow review cadence.

---

## The problem

AI assistants are increasingly asked to test, validate, or operate desktop apps end-to-end. Today they can't. Existing automation tools either require deep app instrumentation (XCUITest, Appium) or simulate user input at a layer too brittle to be trusted (pixel-only click frameworks, headless DOM scrapers). The result: agents that can write apps faster than ever, but can't verify they actually work.

Klyk closes that gap. It gives an AI agent the same input channel a human has — real cursor moves via Apple's CoreGraphics API, real keystrokes posted to the HID event tap, real composited screenshots — and a clean MCP interface to drive it. Native input attempts to stay invisible by default, with the activation and visible-input cases described above. The agent observes, decides, acts, verifies.

## What it does

```
> screenshot the app, then click "Sign in"
[ inspect returns a CoreGraphics screenshot plus a bounded AX element list ]
[ click_element finds "Sign in" via accessibility, then on-device OCR if needed ]
[ Klyk performs an AX action or sends input using the app and session delivery mode ]
```

Tools cover observation, interaction, evaluation, session management, and system operations. Label targeting uses AX → on-device OCR; separate template tools locate previously captured icons or graphics without text. Cross-app drag, right-click-then-select, and multilingual OCR are all first-class. Per-call latency + reasoning-gap metrics so the agent can self-pace. `inspect` combines a screenshot with best-effort AX targeting data in one round-trip; `screenshot` returns only the image.

## Install

```bash
pipx install klyk        # isolated install — recommended
klyk install
```

> **Use `pipx` (or `uv tool install klyk`), not bare `pip`.** Klyk accepts NumPy `>=1.24`; installing it into your global Python can still clash with other packages pinned to different versions. `pipx`/`uv` give klyk its own environment while still putting `klyk` and `klyk-call` on your PATH — same commands, zero blast radius. Plain `pip install klyk` works if you want it in the current environment and accept that risk.

`klyk install` is a turnkey first-run flow:

1. Adds Klyk to `~/.claude.json` (so it appears in every Claude Code session).
2. Walks you through granting the two macOS permissions Klyk needs — opens the exact System Settings panes for **Accessibility** and **Screen Recording**, waits for you to add your terminal app, then **verifies the grant actually came through** before continuing.
3. Runs a final `klyk doctor` pass to confirm every piece is green.
4. Lists other detected AI clients. Use `klyk install --all` to configure them; verify permissions and the connection in each actual client.

For clients that read a natural-language context file (Gemini CLI's `GEMINI.md`), `install --ambient` explicitly opts into a short, marked shell-fallback guide. Normal installation does not edit that file. The guide preserves surrounding content, is refreshed when `--ambient` is repeated, and is removable on uninstall.

Restart Claude Code (or whichever MCP client you use) and klyk is live. Try `inspect Finder` to see it in action. To wire every detected client up front in one shot: `klyk install --all`.

**Troubleshooting: `klyk doctor`.** Run it any time something's off. Reports every dependency, permission, and config grant klyk needs as ✓ / ⚠ / ✗ with the exact next step on anything that's not green. `--json` gives a structured payload for tooling.

### Staying up to date

```bash
klyk update
```

It detects **how** klyk was installed (pipx, `uv tool`, or plain pip), runs the matching upgrade, and then **restarts the running klyk server automatically** — every connected client that shares that installation loads the new version on its next tool call. Clients using pinned, bundled, or separate klyk installations need their own update.

You never have to wonder whether you're behind, either:

- **`klyk doctor`** includes a `klyk version` line — `0.2.0 (latest release)` or `0.2.0 → 0.3.0 available` with the exact command to run.
- **The menu-bar eye** shows a one-line `⬆ Update available` notice when a newer release exists.
- **`klyk update --check`** reports whether an update exists without changing anything.

Behind these sits a single once-a-day HTTPS check of klyk's own PyPI metadata, cached in `~/.klyk/update_check.json` and fully offline-safe. The check sends no screen data or captured content, but the normal network request exposes metadata such as your IP address to PyPI and the network path. Set `KLYK_UPDATE_CHECK=0` to disable it entirely.

**One driver at a time.** Any number of MCP clients can have klyk configured and connected simultaneously (Claude Code *and* OpenCode *and* Cursor…) — each spawns its own klyk process, and none is ever refused. But only **one** session holds the control token and actually drives the Mac at a time, so two agents can never interleave clicks and keystrokes into the same app. A new session takes control automatically when the previous driver is gone; taking over from a *live* driver is an explicit `take_control` call. Exactly one menu-bar eye is visible: the active driver's.

### Use with other MCP clients

`klyk install <client>` auto-configures any supported client — same turnkey flow as Claude (writes the config, grants permissions, runs a health check):

```bash
klyk install opencode    # or: cursor · windsurf · continue · cline · codex · gemini · antigravity (agy) · grok
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
| OpenCode CLI | `~/.config/opencode/opencode.json` or `opencode.jsonc` (legacy `config.json` is honored) |
| Gemini CLI | `~/.gemini/settings.json` |
| Antigravity CLI (`agy`) | `~/.gemini/config/mcp_config.json` |
| Grok CLI (xAI) | `~/.grok/config.toml` |

For OpenCode, setup and verification are two commands:

```bash
klyk install opencode
opencode mcp list        # klyk should show connected
```

Klyk writes OpenCode's global local-MCP entry, so it is available in every workspace and with every provider/model that supports tool use. Existing JSON/JSONC comments, trailing commas, formatting, permissions, providers, and other MCP servers are preserved; re-running the installer is safe, and `klyk uninstall opencode` removes only klyk. The installer finishes by running OpenCode's own MCP status check, catches launcher-specific macOS permission gaps, and does not declare success until OpenCode reports klyk connected. OpenCode normally reloads config changes automatically.

Any other MCP client works too — klyk speaks MCP natively. Add this entry to its config wherever it lives — use the **full path to the Python klyk is installed in** as `command` (run `python -c "import sys;print(sys.executable)"` in that env; `klyk install` fills this in automatically). A bare `python3` only works if klyk is in your global Python:

```json
{ "mcpServers": { "klyk": { "command": "/path/to/python", "args": ["-m", "klyk.mcp_server"] } } }
```

Permissions, control ownership, and `klyk doctor` work identically regardless of which client launches klyk.

### Choosing a model — the speed vs. intelligence trade-off

Klyk is model-agnostic. The time between tool calls and the quality of target selection depend on the driving model, its reasoning settings, and the client. Klyk's own latency varies with the app, accessibility tree, capture size, and action.

Choose a model by testing a representative workflow: check whether it observes before acting, resolves ambiguous targets, and verifies the outcome without repeating consequential actions. A faster model can reduce waiting, but model choice does not replace user authorization or supervision for irreversible work.

### Drive klyk from any AI (no MCP integration required)

Use native MCP when available: the client keeps a server connected and exposes its tool schemas. For agents without native MCP, `klyk-call` provides the same tools over the shell. Each invocation starts and closes its **own** server; it does not share a native MCP session. `--batch` keeps one server alive for its input lines. Batch only predictable steps; observe the UI before deciding subsequent actions.

**`klyk-call` — one shell command, any tool.** For any agent that can run a shell command (great for smaller models — no handshake to reason about):

```bash
klyk-call --list                              # all tools + their parameter names
klyk-call --schema inspect                    # full JSON schema for one tool
klyk-call --tool inspect --app Finder         # call any tool
klyk-call --tool screenshot --app Finder      # screenshot → saved to disk, path returned
echo '{"tool":"screen_info","args":{}}' | klyk-call --batch   # many calls, one session
```

**Antigravity setup:** `klyk install antigravity` (or `klyk install agy`) writes the [documented global config](https://antigravity.google/docs/mcp). Start a new agy session and check `/mcp` to verify the connection; `klyk doctor` verifies configuration and local health, not which tools an existing client session has loaded. Workspace `.agents/mcp_config.json` can also affect the loaded servers. If klyk exists only in the old `~/.gemini/antigravity-cli/mcp_config.json`, doctor reports the mismatch and `klyk doctor --fix` or installation creates the current entry, retaining custom klyk settings and leaving the legacy file intact for older clients. Native MCP needs no `GEMINI.md` instructions; `--ambient` remains an optional shell-fallback guide.

**Control ownership:** a dead driver is reclaimed automatically. Taking over from a live driver requires user authorization. A standalone `klyk-call --tool take_control` ends with that invocation, so it cannot grant control to later shell commands. Use a persistent native MCP connection for interactive workflows.

**Vision over the shell.** When a tool returns a screenshot (`screenshot`, `inspect`, `verdict`, image-producing `run` steps), `klyk-call` writes the PNG to `~/.klyk/captures/` and returns its `saved_path` instead of dumping base64 — so the agent *views* the capture with its own image reader (Claude Code's file read, Gemini's `@path`, etc.) and keeps the same observe→act→verify loop the native MCP transport has, with no context-flooding payload. The cache keeps the most recent 20 captures.

## Quick example

```
inspect(app="Google Chrome")
# → returns the image plus a bounded AX element list

run(app="Google Chrome", actions=[
    {"tool": "click", "x": 580, "y": 389},
    {"tool": "fill_field", "x": 580, "y": 389, "text": "This video is incredible!"},
    {"tool": "screenshot"}
])
# → executes the full sequence at OS speed, returns one batched response

verdict(app="Google Chrome", test_description="Entered the expected text in the comment field")
# → returns final screenshot + logs + grading criteria for the agent to synthesize PASS/FAIL
```

## Why these specific trade-offs

The interesting product decisions weren't tools to build but tools to *not* build:

- **No third-party computer-use libraries.** Everything runs through Apple's CoreGraphics, Vision, and Accessibility frameworks via Python's `ctypes`. Keeps the dependency footprint tiny and the failure surface predictable.
- **No visual grounding models.** UI-TARS and OmniParser would have closed the "no AX, no text" gap — at the cost of a multi-GB model download and 1–2s per call. Rejected. Template matching locates a previously captured visual target without an additional model; it requires a reference crop and does not provide general visual understanding.
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
| `clients.py`, `jsonc.py` | Multi-client setup plus atomic, comment-preserving OpenCode configuration |
| `updates.py` | Update awareness (daily cached PyPI check) + `klyk update` plumbing |
| `keycodes.py`, `logs.py` | Low-level support |

For the full tool reference and behavior contracts, see the tool `description` fields in `klyk/mcp_server.py`. For how the internals are shaped and why, see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Security model & trust scope

Klyk is a thin pipe between the agent and the OS. Its trust model is straightforward:

- **Local control with an optional metadata check.** Klyk does not send screenshots, OCR results, AX labels, or captured screen data in its optional once-daily HTTPS request to PyPI. The request still exposes ordinary network metadata, including your IP address, to PyPI and the network path. Results returned over stdio to the calling agent or client may then be transmitted to that client's model provider under its own settings. Disable the check with `KLYK_UPDATE_CHECK=0` and klyk makes no network calls itself.
- **macOS permissions are the consent surface.** Accessibility and Screen Recording must be granted explicitly via System Settings; `klyk doctor` shows the current state.
- **The agent controls the intent.** Klyk doesn't decide what to click or type — it executes what the agent asks, subject to code-enforced ownership, target-window bounds, duplicate-label rejection, and the emergency latch. Run klyk only with agents you trust to act on your behalf.
- **Stderr from launched apps is captured for the `verdict` payload.** klyk attempts to scrub common credential patterns (passwords, API keys, JWTs, AWS keys, bearer tokens) on a **best-effort** basis — it cannot catch every format, so do not rely on it as your only safeguard.
- **Private framework usage.** Klyk uses Apple's private SkyLight framework for attempted invisible native input delivery (the "autonomous" mode); some browser, shortcut, menu, dialog, and long-press paths may activate or use visible input. This is fine for CLI / PyPI distribution but is the reason klyk can't ship via the Mac App Store.
- **Reporting a vulnerability.** See [`SECURITY.md`](./SECURITY.md).

## License

MIT — see [`LICENSE`](./LICENSE).

## Disclaimer — No Warranty

klyk is provided **"AS IS", without warranty of any kind**, express or implied, including merchantability, fitness for a particular purpose, and non-infringement (see [`LICENSE`](./LICENSE)). To the maximum extent permitted by law, the author is **not liable** for any damage, data loss, financial loss, account action, privacy exposure, or other harm arising from the use, misuse, or malfunction of klyk — whether caused by the software, the AI agent driving it, or a third-party dependency or macOS framework it relies on. Credential scrubbing is best-effort, and confirmation guidance is agent-cooperative; code-enforced bounds, ownership, duplicate-label checks, and the emergency latch do not guarantee safety or approve consequential actions. **You run klyk at your own risk and are solely responsible for what you connect it to and what it does.**

---

*Designed and shipped using AI as implementation partner. The product decisions, scope choices, and trade-offs are mine.*
