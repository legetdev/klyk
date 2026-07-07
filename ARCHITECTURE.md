# Klyk Architecture

Internals reference for working on the klyk codebase. The "why this code is shaped this way." For installation and usage, see [`README.md`](./README.md). For the canonical agent-facing tool reference and behavior contracts, see the tool `description` fields in `klyk/mcp_server.py`.

---

## How It Works

All interaction goes through Apple's CoreGraphics framework via Python `ctypes` — no third-party computer use libraries. The physical cursor moves, real keystrokes fire, and screenshots capture the actual composited screen.

Sessions are keyed by app name. Agents never handle a `session_id` — they just use the app's name:

```python
screenshot(app="Youty")              # launches Youty if not running, returns screenshot
fill_field(app="Youty", x, y, text)  # type into a field
click(app="Youty", x, y)             # click a button
verdict(app="Youty", test_description="...")  # PASS or FAIL
```

The first call to any tool for a given app automatically launches it. Subsequent calls reuse the session.

---

## File Structure

```
klyk/
├── mcp_server.py        # MCP server, tool definitions, handlers
├── session.py           # Session registry, auto-create by app name, template cache
├── computer.py          # CoreGraphics input synthesis (click, drag, keyboard, scroll, AX)
├── capture.py           # CoreGraphics screen capture (CG primary, screencapture fallback)
├── launcher.py          # App launch (browsers get --force-renderer-accessibility)
├── logs.py              # Log capture (native app stderr buffer + reader)
├── ocr.py               # Apple Vision OCR — click_element + accurate-mode fallback
├── matcher.py           # Pure-NumPy template matching — get_template / find_template / wait_for_visual
├── grader.py            # UI grading — platform-specific criteria
├── reporter.py          # Verdict evidence aggregation
├── keycodes.py          # macOS virtual key code table (regular keys)
├── ax_roles.py          # Shared AX role catalogs — INTERACTIVE / BROWSER_INTERACTIVE
├── skylight.py          # Private SkyLight framework binding — invisible mouse path
├── requirements.txt
├── README.md            # Install, permissions, usage
├── ARCHITECTURE.md      # This file — internals reference
├── AGENTS.md            # Orientation for contributors and non-Claude agents
├── SECURITY.md          # Security policy, trust model, vulnerability reporting
└── LICENSE              # MIT
```

---

## Architecture Details

### CoreGraphics via ctypes
All input events use Apple's CoreGraphics framework directly via Python's `ctypes`. No third-party computer use libraries. The "humanoid" mode posts events to `kCGHIDEventTap` — the hardware input tap — so the OS processes them identically to physical input. The default "autonomous" mode and the strict "background" mode route mouse events through SkyLight instead (see Seamless Mode section below).

### Seamless Mode — invisible input via SkyLight
klyk supports three input-delivery modes selectable per session via the `set_mode` tool:

- **autonomous** (default for every new session) — invisible-first for **native apps**. Confirmed empirically (2026-07-06) against independent native apps (TextEdit, Finder), the invisibility split is by event type:
  - **Plain keystrokes (`CGEventPostToPid`) and scroll (`SLEventPostToPid`)** are genuinely invisible **regardless of frontmost state** — no activation is ever needed or performed. Scroll matches the same macOS convention as a real trackpad gesture over a background window (content updates without bringing it forward); keyboard is PID-targeted and never depends on key-window status.
  - **Click, double/triple-click, and drag** are also **fully invisible**. Before posting the SkyLight click, klyk makes the target window the *key* window via `skylight.make_window_key` — yabai's `SLPSPostEventRecordTo` pattern (two stamped ~248-byte event records). A raw SkyLight click into a non-key native window fires simple controls (buttons, menu items) but *not* key-window-dependent ones (text-field caret, list/table/sidebar row selection); making the window key first lets the full range interact **without** activating the app, raising the window, moving the cursor, or changing the OS-active app. Verified 2026-07-06 (6/6 reproducible + real backgrounded TextEdit): a backgrounded window's button and text field both interact after a keyed click while the user's active app and window stack stay exactly put — `via: "skylight+keyed"`. The fuller `_SLPSSetFrontProcessWithOptions` variant (yabai/cua-driver's `focus_window_without_raise`) additionally *steals keyboard focus* and is deliberately **not** used. If the key-window symbols are unavailable on some future macOS, `make_window_key` returns False and the click degrades to a raw post (`via: "skylight"`), which still fires simple controls.
  - **Command shortcuts are the one exception that still needs activation.** macOS routes menu key-equivalents (Cmd+A / Cmd+C / Cmd+V, and thus clipboard *paste*) through the **active** app's menu bar, so a keyed-but-not-active window is not enough (verified 2026-07-06: Cmd+A / paste no-op on a keyed background window). Autonomous briefly activates for exactly these: paste-mode `type_text`, `fill_field`'s clear+paste *fallback* (its primary `AXUIElementSetAttributeValue` path stays invisible), and command-shortcut `press_key`. Per-character (`mode='keys'`) typing needs no activation and stays invisible.
  
  **Chromium-based apps** (browsers and Electron/CEF) are handled differently: their renderer mishandles synthetic events (the trusted-event filter reorders, mis-places, or silently drops them — while the post still reports success, so klyk can't detect the miss). For those, klyk activates the app and uses the **real cursor** for clicks (cursor-warp, which the renderer hit-tests correctly) and activates the window before keystrokes — the brief focus flicker is the documented cost of reliability. Detection is by bundle: `CHROMIUM_BROWSERS` plus the Electron/CEF framework probe in `launcher.is_chromium_renderer_app` (native apps, including Tauri/WebKit, never match). Top priority is task completion — the user sees the effects of klyk's actions in the target window as they happen.
- **background** — same invisible-first preference as autonomous, but BAILS instead of activating. When SkyLight can't deliver, returns `requires_foreground: true` with a reason and the agent decides what to do. Strict "never touch my foreground" mode.
- **humanoid** — cursor-warp behavior. Real cursor moves to the click point, target app becomes frontmost, focus changes. The pre-Phase 2 default; opt in via `set_mode` when the user wants to watch each action visibly.

**SkyLight is a private Apple framework** at `/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight`. klyk `dlopen`s it via `ctypes` and resolves two symbols: `SLEventPostToPid(pid_t, CGEventRef)` for routing and `SLEventSetWindowLocation(CGEventRef, CGPoint)` for stamping the window-local destination. **Risk:** Apple does not document these and could change or remove them in any macOS release. The same APIs underpin Codex Mac's "Background Computer Use" and yabai's window-management daemon; both have shipped against them across multiple macOS versions without breakage, so empirical track record is good, but there is no Apple guarantee. If SkyLight ever fails to load, `skylight.is_available()` returns `False` and klyk falls back to cursor-warp; the user always has a working tool, just visible. See [Known Limitations & Risks](#known-limitations--risks) for the consolidated risk register — including the **silent-delivery-failure** mode that this load-time check does *not* cover.

**Field-stamping recipe (the hard-won detail):** a CGEvent posted via `SLEventPostToPid` is silently dropped unless the following fields are stamped on it before posting. The first seven are universal across event types; the bottom three apply only to specific events (and were added in Phase 2.5):

| Field constant | Slot | Purpose | Used by |
|---|---|---|---|
| `kCGEventTargetUnixProcessID` | 39 | PID to route to | all |
| `kCGEventSourceUnixProcessID` | 41 | Set same as target | all |
| `kCGEventTargetWindow` | 51 | CG window ID | all |
| `kCGMouseEventWindowUnderMousePointer` | 91 | Same window ID | all |
| `kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent` | 92 | Same window ID | all |
| `kCGMouseEventPressure` | 34 | 1.0 for down, 0.0 for up | mouse events |
| `SLEventSetWindowLocation(ev, CGPoint)` | private API | Window-local point | all |
| `kCGMouseEventClickState` | 1 | 2 on the second click pair of a double-click | `post_double_click` |
| `CGEventSetFlags(ev, flags)` | flags field | Modifier mask: Cmd 0x100000, Shift 0x20000, Option 0x80000, Ctrl 0x40000 | any event with modifiers |
| `kCGScrollWheelEventDeltaAxis2` | 12 | Horizontal scroll delta (vertical uses the wheel1 ctor arg directly) | `post_scroll` (horizontal only) |

These constants are publicly documented in `<CGEventTypes.h>` but the requirement that all seven be set together is empirical — discovered by reverse-engineering the open-source clients (cua-driver, groqtalk, openclicky). Without the full set, `SLEventPostToPid` reaches the WindowServer but the WindowServer rejects the event for delivery; no error is raised, the click is just silently dropped. `klyk/skylight.py` encodes the complete recipe in `_stamp_mouse_event` + `_stamp_routing`.

**Coordinate convention:** the public API of `skylight.post_*` takes window-local coords with **top-left origin** — same as everywhere else in klyk. The underlying NSView callback re-reports it in bottom-left coords, but that's an AppKit-side convention; the routing layer interprets top-left. Verified against an in-process AppKit sink across multiple Y offsets.

**Which tools are seamless-aware:**

- ✅ `click(x, y, button, modifiers)` — full seamless coverage including modifier-flag stamping. Cmd+click → open-in-new-tab, Shift+click → range select, etc. all land invisibly. On native apps klyk calls `skylight.make_window_key` first so the click drives key-window-dependent controls without activation or raise (`via: "skylight+keyed"`; see the autonomous-mode split above). (Phase 2 + 2.5 + Phase 9)
- ✅ `double_click(x, y, modifiers)` — two stamped click pairs; the second carries `kCGMouseEventClickState=2` so apps see a real double-click. (Phase 2.5)
- ✅ `drag(x1, y1, x2, y2, button, modifiers)` — stamped mouse-down → ~20 interpolated stamped dragged events → stamped mouse-up, preceded by `make_window_key` (Phase 9). Modifier flags held across the whole sequence. Verified 2026-07-06: a keyed backgrounded drag drives a **modal mouse-tracking control** (NSSlider thumb, 0→100) 3/3 with zero focus theft — the case that most plausibly needed activation, so reorder / resize / canvas drags are covered. *Caveat:* drag-to-**select text** in an NSTextView does not register via synthetic events — but that fails with activation too (a macOS synthetic-drag limitation, not a klyk regression), and text selection is done with double/triple/shift-click anyway. (Phase 2.5 + Phase 9)
- ✅ `scroll(x, y, direction, amount, modifiers)` — stamped scroll-wheel event routed to the target PID. Vertical uses the wheel1 ctor arg; horizontal stamps `kCGScrollWheelEventDeltaAxis2`. Cmd+scroll (zoom) and Shift+scroll (horizontal in some apps) supported via modifier flags. Primer skipped — scroll-wheel events don't share the Chromium renderer-trust filter that clicks hit. Posts directly regardless of frontmost state (fixed 2026-07-02): a backgrounded native-app scroll moves the visible content with zero activation. (Since Phase 9 the click family is invisible too, via `make_window_key`; scroll needs neither activation nor keying.) (Phase 2.5)
- ✅ `click_element(label)` — layered invisible cascade for AX matches: (1) AX action chain (AXPress → AXOpen) on the matched element; (2) same chain walked up to 2 parent levels, since Finder sidebar rows expose AXOpen on AXRow not on the inner AXStaticText; (3) SkyLight click at the matched element's coords when no AX action lands. OCR fallback uses SkyLight too. (Phase 2)
- ✅ `fill_field(x, y, text)` — two-stage cascade: (1) `ax_set_value` — pure AXUIElementSetAttributeValue write, **fully invisible** (no cursor / keyboard / clipboard / activation), native macOS text inputs only (web inputs detected via AXWebArea ancestor walk and skipped) — the common native path; (2) fallback when the AX write can't apply (mostly web/Electron fields): focus-click + Cmd+A + paste, which are menu shortcuts requiring the frontmost app, so autonomous **activates** first (`via: 'activated'`) and background returns `requires_foreground`. Response carries `via: 'ax_set_value' | 'activated'` and an `ax_skip_reason` when the AX path bailed (web_backed, not_text_input, not_settable, no_element). (Phase 2 + 2.5 + Phase 9)
- ✅ `screenshot` — skips `activate_app` and `AXRaise` in seamless modes (capture is z-order independent regardless). (Phase 2)
- ✅ Keyboard tools (`press_key`, `type_text`, `hold_key`) — plain `CGEventPostToPid` keystrokes are invisible for native apps regardless of frontmost state. Two cases need the target frontmost, so autonomous activates (background bails with `requires_foreground`): (a) Chromium apps drop background keydowns; (b) **command shortcuts / clipboard paste** (Cmd+…), which macOS routes through the active app's menu bar — this includes `type_text` **paste mode** (its native default) and any `press_key` Cmd combo. `type_text mode='keys'` (per-char) stays fully invisible on native apps and is the default on Chromium (keydown-driven web UIs ignore paste). (Phase 2 + later + Phase 9)

**Response contract:** every click-class response in seamless modes includes a `via` field — `"skylight+keyed"` (native click after `make_window_key` — the default invisible path), `"skylight"` (raw post; e.g. scroll, or when the key-window helper is unavailable), `"skylight+primer"` (Chromium), `"activated"` (fill_field's clear+paste fallback), `"ax_set_value"`, `"ax_action"` (with `action` = which AX action fired and `level` = "element" / "parent_1" / "parent_2"), `"ax_match+skylight"` (+`"+primer"` for Chromium — AX matched the label but no AX action worked, so SkyLight delivered at the matched coords), `"ocr+skylight"`, or `"cursor_warp"` — so the agent always knows which delivery path ran. Background-mode failures return `{ok: false, requires_foreground: true, reason: <specific>, suggestion: <next step>}`. Autonomous-mode escalations carry `escalated_from: <error_token>` on the cursor-warp response so the agent (or a later human review of `get_escalation_log`) can tell escalation cursor-warps apart from humanoid-mode cursor-warps. The token is either a genuine SkyLight failure (e.g. `activation_failed`) or — for Chromium-based apps — `chromium_cursor_warp`, meaning klyk deliberately chose the real cursor because the renderer can't be trusted with synthetic clicks.

### Drag implementation
Humanoid-mode drag uses `CGEventCreateMouseEvent` with `kCGEventLeftMouseDragged` through 20 interpolated intermediate points, posted via the global HID tap. Seamless-mode drag uses the same interpolation but each event is fully stamped (routing fields + window-local point + modifier flags) and posted via `SLEventPostToPid` — same Phase 2 recipe extended to the dragged event type. Smooth interpolation is required either way — macOS apps that track drag events (list reordering, sliders, window resize) need continuous position updates, not a teleport.

### Long press
`long_press(x, y, duration, button)` is a mousedown → hold → mouseup pair sharing the same `_input_lock` as click. The hold loop sleeps in 50 ms increments and re-checks `_check_stop()` between increments so a long press (default 1 s, up to 10 s) doesn't swallow the emergency-stop chord (Cmd+Shift+Escape). Distinct from `drag` because the cursor stays put — the use case is "hold-to-arm" controls (context menus on hold, springboard menus, drag-handles that activate after a delay).

### AX action invocation
`ax_action(x, y, action)` resolves the AX element at the coordinate via `AXUIElementCopyElementAtPosition` on the system-wide AX, reads its supported actions via `AXUIElementCopyActionNames`, and invokes `AXUIElementPerformAction` with the requested action string. The pre-check against the supported-actions list means a wrong action returns `status: "unsupported"` + the full `available_actions` list in one round-trip — no need for the agent to discover capabilities by trial-and-error. Use cases: tiny hit areas, dynamic layouts, accessibility-focused apps that respond cleanly to AX but oddly to synthetic clicks. The implementation lives in `computer.ax_perform_action_at` and uses the same `_ax_perform_action` primitive that `raise_window` already calls for `AXRaise`.

### System / media keys
`press_system_key(name)` covers volume, mute, brightness, play/pause, track skip, keyboard backlight, and eject. These keys are NOT in the regular `CGEventCreateKeyboardEvent` table — the OS routes them through `NX_SYSDEFINED` (NSEventType 14) with subtype 8 (aux-key) and a packed `data1` field: `(code << 16) | (phase << 8)` where `phase` is `0xA` for down or `0xB` for up. The event has to be constructed via `NSEvent.otherEventWithType_...` (AppKit) because the CGEvent C API can't change event type post-creation; we then call `.CGEvent()` on the NSEvent and post via `CGEventPost(kCGHIDEventTap, ...)`. AppKit is lazy-imported so the cost is paid only when this tool is used. The keys are global — they affect the OS, not the foreground app — so the `app` parameter exists only for session-continuity logging, not routing. Names map to the IOKit `NX_KEYTYPE_*` constants from `<IOKit/hidsystem/ev_keymap.h>` (e.g. `volume_up` → code 0, `play_pause` → code 16).

### Batch key sequences
`press_key` accepts an optional `keys[]` (ordered sequence) and `repeat` (loop count) in addition to the single-`key` form. When either is set, the handler builds the full key list, enforces a 1000-press hard cap, performs **one** `_focus_if_needed` for the whole batch, then calls `computer.press_keys` which parses every entry up front (atomic-fail on a bad key string — no partial side effects) and replays the sequence under a single `_input_lock` acquisition. The MCP-level response stays `{ok: True}` so `run`'s boring-collapse still merges adjacent batches into `press_key×N` summaries. Net effect: a 200-keystroke blind sequence costs one tool action, one lock acquire, one focus raise — instead of 200 actions × per-call overhead. The trade-off is reactivity (Consideration #6): the batch is blind to mid-flight popups, so only use it when intermediate state is predictable from the pre-batch screenshot.

### Composited screenshots
Primary path: `CGWindowListCreateImage` with a global-coords rect → ImageIO encodes to PNG entirely in memory (~40 ms, no subprocesses). On Retina, the result is downscaled to logical points via `CGBitmapContextCreate` so coords always match the screenshot's pixel grid. Fallback: `screencapture -R x,y,w,h` + `sips -Z` for resize if the CG path fails or ImageIO isn't loadable. Both paths composite the window against the desktop background, so vibrancy, transparency, and blur effects render correctly.

### Session management
Sessions are stored in a registry keyed by app name string. `get_or_create_session` either returns the existing session (refreshing window bounds) or launches the app and waits for its window. The `session_id` exists internally but is never exposed to agents.

### App activation
`activate_app(pid)` uses the native ProcessManager APIs (`GetProcessForPID` + `SetFrontProcessWithOptions` with `FrontWindowOnly`) — ~5 ms vs ~450 ms for osascript. Brings the app's front window to focus so it receives keyboard input. Falls back to a shortened osascript (`set frontmost`) if the ProcessManager call fails. Keyboard events use `CGEventPostToPid` to route directly to the process queue, so most keyboard tools don't need a prior activate call at all.

### ax_value retry
SwiftUI's `@State` / `@Binding` propagation to the accessibility layer takes 200–800ms after a paste event. `ax_value_at` retries up to 4 times with 150ms delays, returning the first non-empty result.

### Browser AX strategy
Chromium-family browsers (Chrome, Edge, Brave, Arc, Vivaldi, Opera, Chromium) launched by Klyk get the `--force-renderer-accessibility` flag so their full web AX tree is exposed — without this flag Chromium ships a11y disabled until a screen reader is detected. Safari exposes web content to AX by default. The forced tree is filtered at the MCP layer to interactive roles only (`AXButton`, `AXLink`, `AXTextField`, …) so the agent isn't drowned in `<span>` and `<div>` noise.

The flag only applies on cold launch — if the user already has the browser running, the flag is silently dropped by `open -a`. `launcher.launch_native_app` detects this case via a quick pre-launch PID lookup (`_quick_pid_for_app`) and returns `was_already_running=True`; `create_session` sets `Session.ax_disabled` for Chromium browsers in that state, and the `screenshot` / `ax_snapshot` handlers attach an `ax_disabled_warning` to every response. Without that warning, an agent would see an empty web AX tree, fall through to OCR on every `click_element` call, and have no idea why — the warning is the canonical signal that the user must quit and reopen the browser to re-enable web AX.

### OCR fallback
`click_element` falls back to Apple's Vision framework (`VNRecognizeTextRequest`) when the AX tree has no match. Runs on-device via PyObjC, ~50–150 ms per full-window screenshot, no model download. Catches anything rendered as visible text in canvas surfaces, Electron apps, or browser content where AX comes up empty. The fallback is invisible to the agent — `click_element` returns `via: "ocr"` to indicate which path hit.

### Template matching
`get_template` / `find_template` use a normalized cross-correlation (the `TM_CCOEFF_NORMED` metric), implemented in **pure NumPy** — the numerator via an FFT (5-smooth-padded for speed), the per-window sums for zero-normalization via integral images. PNG decode/encode go through klyk's first-party CoreGraphics codec (`capture.decode_png_to_rgb_array` / `encode_rgb_array_to_png_b64`). There is no OpenCV dependency: it was the only package forcing `numpy>=2`, which broke shared Python environments. The result is bit-for-bit equivalent to OpenCV's `matchTemplate` (verified elementwise, max|Δ|≈2.6e-7); a full-window (1800×1169) match runs in ~430 ms, an order of magnitude faster with a `search_region`. The reason this is exposed as two explicit tools (rather than folded into `click_element`) is that it needs the agent to first identify a region from one screenshot, then locate it later — a different mental model from "find by label." Use it for icons, custom graphics, and anything else without text.

### Template cache
`get_template` stores the captured PNG in `session.template_cache` (per-session dict, cap 50, FIFO eviction) and returns a short `template_id` like `tpl_abc123`. `find_template` and `wait_for_visual` accept either `template_id` or raw `template_b64`. The cache exists because large base64 PNGs are fragile when the LLM transcribes them — the short id is the safer reference. On miss, the error names the cache and tells the agent how to refresh.

### Screenshot + AX folding
`screenshot` returns the image plus, by default, a filtered AX element list capped at 50 entries (`ax_elements`, `ax_element_count`, `ax_truncated` when over cap). The AX read is best-effort: if it fails, `ax_elements=[]` and `ax_error` is set — the screenshot itself never fails. Canonical truth: the image is the source for visual state; the AX list is a reference for label-based targeting.

The AX walk dominates observation latency. On Chromium with `--force-renderer-accessibility` (or any heavy Electron tree) the AX walk takes ~1.3–1.9 s; the capture path itself (`take_screenshot` + ImageIO encode) is roughly 50–150 ms, the rest is AX traversal + filter. Two distinct tools encode this trade-off so the agent doesn't have to remember a flag: `inspect` returns image + AX list (the default for ~95% of observations, since most observations are followed by a label-based action); `screenshot` returns image only for the ~5% of genuinely pure-visual cases (diagnostics, UI evaluation, before/after captures). The tool name carries the intent, removing one opt-in/opt-out from the agent's decision surface.

Pass `save_path` to write the PNG to disk instead of returning it inline. The path is expanded (`~`-relative is fine) and resolved to an absolute path; on success the response gains `saved_path` and the inline image is omitted (saves tokens when the agent only needs the file). On write failure the response still contains the inline image plus a `save_error` string — the screenshot itself doesn't fail. The parent directory must already exist; klyk doesn't auto-create.

### Click hint
After every `click(x, y)`, the server scans the AX tree for any labeled element within 20 px of the requested point. If one exists, the response gains a `nearby_ax_hint` block (label, role, distance, suggestion to use `click_element` instead). Logged identically. Costs one AX snapshot per click; only affects `click`, not `click_element`.

### OCR three-tier fallback + recovery candidates
`click_element`'s OCR path runs fast-mode Vision first (~50 ms, drops small or thin text). If that yields no substring match, it re-runs in accurate mode (slower, catches sub-15 px links and icon captions). If accurate mode still has no substring match, a third tier compares the query to each observation with **all whitespace collapsed**, accepting only an *exact* stripped-equality hit — this rescues a single rendered word Vision fragmented across a stray gap (`"ENTER"` → `"EN TER"`) without ever widening matching to unrelated text (exact-only, never substring). The match tier is reported in `via`: `"ocr"` for the substring tiers, `"ocr_despaced"` for the whitespace tier. All passes use Apple Vision (`VNRecognizeTextRequest`) on-device.

When every tier (AX + all three OCR tiers) misses, `click_element` does not dead-end. It builds `visible_text_candidates` from the OCR observations it already captured — the closest on-screen strings ranked by `difflib` similarity (best of raw vs whitespace-collapsed), each with window-relative `x`/`y` and a `similarity` score — plus a `hint`. This converts a silent "not found" into a recoverable step: the agent retries with the exact rendered spelling or clicks the returned coordinates directly, instead of looping blind. Critical for small/fast models on web/Electron surfaces with a thin AX tree. Pure in-memory ranking over the existing capture — no extra OCR or IPC. (Design Considerations #2 fail-loudly, #10 return-enough-evidence.)

### Hyphen normalization
A `_normalize_label` translation maps U+2010–U+2014 and U+2212 to ASCII `-` before comparing labels. Without this, a literal `"Wi-Fi"` from the agent fails to match macOS's `"Wi‑Fi"` (U+2011 non-breaking hyphen). Applied in both AX and OCR matching paths.

### wait_for_visual polling
Polls `screenshot + matcher.find` at `poll_interval` until the template appears (or disappears, if `present=false`). Effective cycle is `poll_interval + screenshot+match cost (~330 ms)` — the parameter doesn't override the floor. On every poll, exceptions are caught, logged, and surfaced as `last_error` if the call eventually times out, so a single transient failure never aborts the wait.

### Per-call timing and logging
Every tool call logs `tool: <name>` on entry and `done: <name> duration_ms=N gap_ms=N` on exit (gap = wall time since the previous top-level call returned, approximating LLM reasoning time). Nested calls inside `run` are tagged `nested=1` and don't update the gap anchor. Top-level responses get a `_meta: {duration_ms, gap_ms}` block injected into the trailing JSON payload. Logs roll at 10 MB, 5 backups, in `~/klyk.log`.

### Pixel sampling
Both `get_pixel` and `get_pixels` capture from the **specific target window's content** via `CGWindowListCreateImage(..., kCGWindowListOptionIncludingWindow, window_id, kCGWindowImageBoundsIgnoreFraming)` — NOT from the composited desktop. This means overlapping windows on top of the target don't affect the result: z-order is irrelevant, the target window does not need to be raised, and user-visible state is undisturbed. Without this, a covered window would silently return the covering window's pixels — the bug `get_pixel` carried until this design.

`get_pixel` captures the target window and reads pixel bytes directly from the CGImage's data provider (`CGImageGetDataProvider` → `CGDataProviderCopyData` → `CFDataGetBytePtr`) — no intermediate bitmap context. ~5 ms per call. Coordinates are window-relative, consistent with the rest of the toolkit; the handler resolves the window via the same `_resolve_window` / `_refresh_window` path as `screenshot` and `click`, so passing `window="A"` to `get_pixel` lands in the same window that `screenshot(window="A")` shows. Retina is detected by comparing `CGImageGetWidth` to the logical window width and scaling the read coordinate accordingly.

`get_pixels` is the batch variant: one window capture, N reads from the same CGImage. The capture dominates cost (~40 ms), so per-point overhead drops to microseconds. Worth using from ~3 points onward — Wordle tile grids, calendar heatmaps, multi-LED status panels, any matrix-of-colors decision. All points (and rects) are validated against window bounds before the capture fires, so a single off-window coord fails fast with a clear error rather than corrupting the batch.

`get_pixels` exposes two complementary sampling modes from the same capture: `points` for exact 1×1 reads and `regions` for per-channel median over a rect. The region path exists because the natural way to "read the color of a cell" — sample the center of the rect — collides with whatever glyph or icon usually sits there (the white letter inside a Wordle tile, the icon inside a status badge). The first version of this workflow forced the agent to guess an offset that dodged the glyph, then retry when the guess hit white. Median sampling over the rect dissolves the problem: with ~256 strided samples per rect, the glyph occupies a minority of the histogram and the per-channel median lands on the surrounding fill. Implementation is `_read_rect_medians_from_cgimage` — same data-provider path as the single-pixel reader, same channel-format resolution, ~16-column × ~16-row stride per rect so the cost stays sub-millisecond regardless of rect size. Both modes may be passed in one call and share one capture.

Why the direct data-provider path instead of rendering into a fresh bitmap context first: earlier versions did exactly that and hit a stack of silent corruptions. (a) The project's `_kCGBitmapByteOrder32Host` constant was set to `0x4000`, which is actually `kCGBitmapByteOrder32Big` — on every Apple Mac (all little-endian) this meant the rendered buffer ended up ARGB-in-memory while the reader assumed BGRA, returning permuted channels. The screenshot path stayed visually correct because the PNG encoder reads the bitmap-info flags before interpreting bytes; only the byte-level readers (`get_pixel`, `get_pixels`) saw garbage. (b) `CGBitmapContext` is bottom-left-origin while `CGWindowListCreateImage` returns top-left-origin images, so without a CTM y-flip the buffer was upside-down. (c) Retina capture returns a 2×-resolution image but the bitmap context was sized to logical points, masking the scale issue. The direct read path skips all three: it inspects the captured CGImage's actual `CGImageGetBitmapInfo` + `CGImageGetAlphaInfo` to pick the right channel offsets (handles BGRA-little, ARGB-big, RGBA-little, RGBA-big), and reads with the source image's own `bytesPerRow` stride. One truthful color reader, four macOS pixel formats handled explicitly.

### Multi-window targeting (CG ↔ AX bridge)
`list_windows` enumerates an app's windows via `CGWindowListCopyWindowInfo` (filtered to layer-0, on-screen, ≥ 50 px), returned in z-order (frontmost first). Each entry carries the CG `window_id`. AX-side operations (`AXRaise`, `AXPosition`, `AXSize`) need an `AXWindow` ref, which the AX API doesn't connect to CG IDs directly. The bridge in `_ax_window_for_cg_id` tries four passes, strongest signal first:

1. **Position + size both match unique** — strongest signal; disambiguates a fullscreen overlay window that shares an origin with a smaller quadrant window (e.g. fullscreen at `(0,30) 1920×1050` vs. quadrant `B` at `(0,30) 960×540`).
2. **Position-only match unique** — used when size isn't available (mid-animation, AX not yet caught up).
3. **Size-only match unique** — useful when the window is animating to a new origin but its size is settled.
4. **Z-order pairing** — fall back to "Nth CG window = Nth AX window in front-to-back order" only when none of the above produced a unique match.

Stale window IDs (closed/relaunched) surface as a clear error naming the missing window — the agent calls `list_windows` to refresh. macOS-native fullscreen windows live in their own Space and resist AX bounds changes; the tool returns ok but the window doesn't move. Exit fullscreen first (`Ctrl+Cmd+F` in Chrome) before tiling such windows.

`raise_window` verifies post-condition: after `AXRaise`, it reads the app's `AXFocusedWindow` and checks its position+size against the target. If the focused window doesn't match, it retries once with a longer settle (80 ms). The return shape always includes `focused: bool` and (when `focused=false`) a `warning` string. The four `via` values communicate where the call ended up:

| `via`             | Meaning                                                                   |
|-------------------|---------------------------------------------------------------------------|
| `ax`              | AXRaise worked first try, target is now key window                        |
| `ax_retry`        | AXRaise needed a retry but the target is now key window                   |
| `ax_no_match`     | Could not resolve a CG id → AXWindow ref (target may already be key)      |
| `ax_raise_failed` | AXRaise returned but the focused window is still something else           |

`_focus_if_needed` (mcp_server) returns this status dict so every tool handler can attach a `focus_warning` field to its response when `focused=false`. `click`, `press_key`, `screenshot`, `run`, `focus_window` all surface this. Inside `run`, any inner action's warning is also aggregated into a top-level `focus_warnings` array so agents skimming the summary still see it. The agent contract: **treat `focus_warning` as a hard failure of that step** — the keystroke or click landed in some other window of the same app — and stop, dismiss whatever stole focus (typically a modal in another window), then retry. Do not assume later steps succeeded just because they returned `ok=true`.

Multi-window driving: every action tool (`screenshot`, `click`, `press_key`, `set_window_bounds`, `run`) takes an optional `window` (A/B/C label) or `window_id` (raw CG id). When set, the tool raises that window via AX before acting and uses its bounds for coordinate translation. Inside `run`, a top-level `window`/`window_id` cascades to every action that doesn't override it; per-action `window`/`window_id` lets you interleave operations across multiple windows in one MCP call. For four Chrome windows tiled in quadrants, a single `run` with interleaved `Cmd+\`` + arrow keys cycles through all four windows in ~50ms total — true "simultaneous" play from a human-perception standpoint without per-window round-trip cost.

---

## Build Order (for fresh-build reference)

Historical layering — modules at the top have no dependencies on those below, so this is the order to build/verify if rebuilding from scratch.

1. `keycodes.py`, `ax_roles.py` — static catalogs, no dependencies
2. `computer.py` — CoreGraphics synthesis + AX tree reads; smoke-test with a standalone cursor move script
3. `capture.py` — CoreGraphics screen capture; verify window listing and screenshot
4. `launcher.py` — app launch via subprocess (with browser detection / Chrome flag)
5. `logs.py` — log buffer and stderr capture
6. `session.py` — session registry and `get_or_create_session`
7. `ocr.py` — Apple Vision OCR via PyObjC; smoke-test on a screenshot
8. `matcher.py` — pure-NumPy template matching (FFT + integral-image NCC); smoke-test with a known crop
9. `grader.py` + `reporter.py` — evaluation tools
10. `mcp_server.py` — ties everything together; verify with `tools/list`

---

## Known Limitations & Risks

The single authoritative register of every known limitation and operational risk in klyk. Other docs link here rather than restating it. Ordered by severity: operational risks (things that can fail in production) first, then dependency and maturity caveats, then a pointer to the API-level hard limits tabled below.

### 1. SkyLight private-API dependency — highest risk

**What depends on it.** The invisible-input differentiator. The `autonomous` (default) and `background` modes route every mouse click, scroll, and drag through SkyLight's private `SLEventPostToPid` / `SLEventSetWindowLocation` symbols so the cursor never moves, focus never changes, and the target window is never raised. Keyboard input does **not** depend on SkyLight — it uses the public `CGEventPostToPid`.

**Why it's a risk.** SkyLight is an undocumented Apple private framework (`/System/Library/PrivateFrameworks/SkyLight.framework`). Apple publishes no API contract: the symbols, their argument order, and the exact set of CGEvent fields that must be stamped for delivery can change or disappear in any macOS release, with no notice and no SLA. The required field-stamping recipe (`klyk/skylight.py` → `_stamp_routing` + `_stamp_mouse_event`) is reverse-engineered from open-source clients, not documented by Apple.

**Empirical track record.** yabai and OpenAI's Codex Mac "Background Computer Use" ship against the same APIs across many macOS versions without breakage. The path is empirically stable — but that is a track record, not a guarantee.

**Fallback on load failure — handled.** If the framework or its symbols fail to load at import, `skylight.is_available()` returns `False`, every `post_*` returns `False`, and the caller reverts to the visible cursor-warp path. The tool keeps working; it only loses invisibility (the cursor visibly moves, like a conventional computer-use tool).

**⚠️ Silent-delivery-failure gap — detected by a delivery self-test.** `is_available()` only detects a *load* failure. If a future macOS keeps the symbols loadable but changes delivery semantics or the required stamp fields, `SLEventPostToPid` still reaches the WindowServer but the event is **silently dropped** — no exception is raised, `is_available()` still returns `True`, and the post reports success while nothing happened on screen.

klyk guards against this with a **delivery self-test** (`skylight.self_test()`): it drives a bounded `NSApp.run()` loop, posts a fully-stamped click to an **off-screen** in-process AppKit sink through the real `SLEventPostToPid` path, and confirms the sink's view received it. It runs in two places — once at **server startup** (populating `skylight.delivery_verified()` before the first tool call) and on demand in **`klyk doctor`** ("SkyLight delivery" check). When the self-test finds delivery broken, `delivery_verified()` returns `False` and the seamless chokepoint (`_seamless_post`) skips SkyLight: **autonomous** mode falls back to the visible cursor (`escalated_from: "skylight_delivery_unavailable"`), **background** mode returns `requires_foreground`. So a broken private API degrades to "visible but working" instead of "silent no-op." The self-test is non-disruptive — the sink is parked far off any display, klyk is never activated, the cursor never moves — and self-bounding (a finish timer always stops the loop), so it can't hang or flash at boot.
- *Residual gap (intentional):* the self-test runs at startup, not before every click — an OS change that breaks delivery *mid-session* isn't caught until the next restart or `doctor` run. Per-action delivery confirmation stays unbuilt on purpose: it would add latency to every click to catch a failure mode that only changes across an OS update. The look → act → verify workflow (screenshot / `inspect` after acting) remains the agent-level backstop within a session.

### 2. Browser / Chromium web content is not invisible

Chromium's renderer "trusted-event" filter reorders, mislocates, or silently discards SkyLight-synthesized clicks delivered to a backgrounded window — and the post still reports success, so klyk cannot detect the miss from the return value. To act reliably inside browser or Electron **web content**, klyk therefore (a) briefly brings the browser to the foreground (a visible focus flicker) and (b) prepends a reverse-engineered "primer" click at window-local `(-1, -1)` + 50 ms before the real click. Keystrokes to a background Chromium window are dropped, so the app is activated first.

**Consequence.** The "fully invisible" guarantee holds for **native** macOS surfaces only — AppKit, WebKit, Tauri, system dialogs, the Dock, the menu bar, and Electron app *chrome* (toolbars, menus, sidebars). It does **not** hold for web content rendered inside a browser or an Electron web view. For browser web content, prefer a DOM-level driver (Playwright / CDP).

**Risk.** The primer recipe and the foreground requirement are empirical; a Chromium update could change the filter and break web-content clicks without warning.

### 3. Permissions (TCC) dependency

klyk requires macOS **Accessibility** and **Screen Recording** grants; without both, all input synthesis and screen capture fail. The grant is bound to the specific binary/interpreter that launches klyk — so if you drive klyk from a different launcher than the one you granted (e.g. a new terminal app), the grant won't carry over and you'll need to add that binary too.

### 4. Dependency & environment caveats

- **Native deps.** Apple Vision (OCR) + PyObjC + NumPy. Template matching is pure NumPy and the PNG codec is first-party CoreGraphics, so klyk no longer bundles OpenCV — which was the **sole** dependency forcing `numpy>=2` and breaking environments pinned to `numpy<2` (manim / scipy / moderngl stacks). That conflict is resolved: klyk now accepts `numpy>=1.24` (1.x or 2.x). An isolated venv remains good hygiene but is no longer required to protect a `numpy<2` stack.
- **macOS-only.** All input and capture is darwin-specific (PyObjC + CoreGraphics + SkyLight). There is no Windows / Linux path — the addressable surface is macOS by design.

### 5. Maturity caveat — early, but core paths live-verified

klyk is early and experimental. The core input paths have been live-verified end-to-end against real macOS apps — most recently the Phase 9 invisible-click work (2026-07-06): keyed clicks, `fill_field`, and keys/paste typing tested against backgrounded native apps with reproducible before/after measurement. The invisible-input differentiator rests on **undocumented Apple private symbols** (`SLEventPostToPid`, `SLEventSetWindowLocation`, `SLPSPostEventRecordTo`, `GetProcessForPID`); each is empirically stable and guarded by a load check + delivery self-test with a clean visible-cursor fallback, but Apple offers no contract, so a future macOS could change delivery semantics (see risk #1). Treat exotic or rarely-exercised control types as unproven until they've fired against a real app — the look → act → verify workflow (screenshot / `inspect` after acting) is the standing backstop.

### API-level hard limits

Capabilities that will never ship from this surface area — a macOS API constraint, not a roadmap gap — are tabled in **Hard limits — won't build** immediately below.

---

## Rejected & deferred capabilities

Distinct from the API-impossible "Hard limits" below: these are capabilities klyk *could* build but
deliberately doesn't, on cost/benefit grounds. Recorded so the decisions aren't re-litigated.

**Performance shortcuts (rejected — the bottleneck they target doesn't exist).**
- *AX subtree caching* — the first walk is already ~70 ms; a cross-call cache would buy that 70 ms at the cost of the agent clicking a stale "ghost" element.
- *Delta AX* — reduces token cost, not latency; the walker still has to traverse the tree to detect changes.
- *Lazy / async AX* — breaks the "one tool = one observation" model for the same ~70 ms the parallel `inspect` already saves.
- *Window-bounded AX* — single-window apps don't benefit; multi-window apps already steer via `window_id`.
- *Per-app capability cache* — saves 10–30 ms per `click_element` cascade miss, but adds per-session hidden state and risks caching a transient failure as permanent.

**Tools considered and skipped.**
- *`wait_for_idle`* — high lazy-fallback risk (agents reach for it instead of finding the right readiness signal); no documented failure motivated it.
- *`middle_click`, `cursor_position`, `zoom`* — niche, or already approximated by existing primitives (modifier+click, `move_cursor`, `read_grid` / `get_pixels`).

**Anthropic Computer-Use patterns deliberately NOT adopted** — they contradict klyk's "uniform reach, invisible-first" premise.
- *Tier-based app restrictions* (browsers read-only, IDEs click-only) — contradicts "if a human can do it on a Mac, klyk can."
- *Visible-cursor-only delivery* — would gut autonomous + background modes, klyk's biggest advantage.
- *Per-app `request_access` permission model* — substantial surface against klyk's developer-machine framing; deferred until a concrete use case demands per-app gating.

**No HTTP bridge / remote MCP.** Local MCP clients use stdio directly; a localhost bridge or tunnel to reach hosted apps (ChatGPT, Grok web) would expose full machine control over the network — pure attack surface for a single-machine, local-first tool. The shell front door (`klyk-call`) covers harnesses with weak MCP support without it.

**Built or considered, then removed.**
- *Per-action ghost-cursor sprite* — a small sprite marking each action; removed because it read poorly from peripheral vision, was easy to miss, and competed with the menu-bar item. The always-on menu-bar status item + automatic dock badges replaced it.
- *Auto-pause on user activity* — pausing an autonomous run when the user returns to the keyboard; dropped because klyk tasks complete in seconds (the long-running-agent premise didn't hold) and the Cmd+Shift+Esc emergency-stop already covers "halt now." Would have added session state for a workflow nobody hit.

---

## Hard limits — won't build

Constraints that come from the macOS APIs klyk is built on. Not roadmap items — these will not ship from this surface area.

| Capability | Why not |
|---|---|
| Multi-touch gestures (pinch, rotate, swipe spaces) | Requires the IOKit HID layer; the CGEvent API doesn't support multi-pointer input. |
| Force click | Requires pressure data in IOKit events; not exposed through CGEvent. |
| Touch Bar | Separate input layer; irrelevant for app testing. |
| Login / lock screen | The OS blocks event injection below the login session — a security guarantee, not a bug. |
