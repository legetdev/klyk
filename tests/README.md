# Tests

Run the portable regression suite with the installed project dependencies:

```sh
python3 -B -m unittest discover -s tests
```

The portable tests cover validation, batch failures, targeting, emergency-latch enforcement, input cleanup, Unicode and clipboard preservation, ownership, session limits, configuration editing, and publication privacy. Native boundaries are controlled fakes; these tests do not prove real macOS input delivery.

Focused regressions also exercise real subprocess pipes (large requests, noisy stderr, partial responses, timeouts, and cleanup), failed atomic configuration/cache writes, capture-scope refusal, cancelled click pairs, and save-result evidence. These run without opening apps or operating the desktop.

## Verification while the owner uses the Mac

Portable tests operate on temporary files, controlled native adapters, and ordinary Python subprocesses. They do not launch apps, post input, or change the clipboard. A normal MCP launch installs a visible menu-bar item and performs an off-screen input self-test; a normal connection smoke check therefore does not meet an instruction to remain completely invisible.

The `silent_protocol_smoke.py` check runs the actual package entry point and installed MCP SDK through real subprocess pipes, with real metadata-only permission checks. Its external test bootstrap prevents AppKit initialization, the status item, event-tap installation, input delivery, clipboard access, app activation, subprocess launches, and screen capture. It can check tool discovery, screen metadata, rejected requests, diagnostic privacy, and process cleanup. It explicitly records every substituted boundary. This is protocol integration evidence and cannot satisfy native desktop acceptance.

```sh
python3 -B tests/silent_protocol_smoke.py --output .verification/silent-protocol.json
python3 -B tests/silent_lifecycle_smoke.py --output .verification/silent-lifecycle.json
```

The lifecycle check adds real EOF, SIGTERM, and SIGINT against disposable guarded server children. It uses the production held-input registry with inert keyboard, mouse, and media callbacks to observe release order, exactly-once cleanup, refusal of queued input, and child reaping. Clipboard restoration and native event delivery remain mocked boundaries; this is process-lifecycle integration evidence.

After installing a candidate wheel in a fresh environment, add `--installed` to either silent check and run with that environment's interpreter. This selects the installed client and server, requires the package to live inside that environment, and compares every runtime Python file with the reviewed checkout before launching the child. A source import or an older/different installed artifact fails before protocol calls.

Native ImageIO and Vision checks on generated, static images can run invisibly when they neither start AppKit nor capture the owner's screen. Existing `live_smoke.py`, `desktop_smoke.py`, and the development branch's `background_smoke.py` create real windows; none is suitable while an owner has prohibited visible or audible computer changes. Keep major desktop candidates unpublished until the required real acceptance can run within the owner's constraints.

## Real Mac verification

For a low-interruption pass, use the background-only suite:

```sh
python3 -B tests/background_smoke.py --output .verification/background.json
```

It uses a separate ownership token and two disposable, overlapping AppKit windows. It checks real MCP observations, native input, field readback, OCR regions, grid text, foreground preservation and warm timings against independent fixture state. User app switching is allowed; bringing a test window or server to the foreground fails the check. It does not operate user apps, exercise browser foreground fallbacks, or substitute for the full major-release gate below.

The opt-in native check compiles `Fixture.swift` using `xcrun swiftc`, starts disposable AppKit apps, and drives all 48 tools through the real stdio MCP server:

```sh
python3 -B tests/live_smoke.py --output .verification/native.json
python3 -B tests/desktop_smoke.py --output .verification/desktop.json
```

These checks actively operate the desktop. Run them on a Mac available for testing, with Accessibility and Screen Recording permission for the runner. The native check uses disposable windows, text, files, menus, and dialogs. The desktop check requires Chrome and Visual Studio Code already installed; it creates a local browser page and isolated browser/editor processes and profiles. Chrome is launched through its own executable with a disposable profile, so the fixture needs no extra Chrome AppleEvents grant. An existing Chrome or Code process prevents isolated app-name targeting and stops the check. The exact owned process, selected window, and fixture document are verified before input. The owned Chrome/Electron fixtures request renderer accessibility; cold-renderer readiness uses bounded fresh observations, and input actions are never retried by that wait. No paid model or remote test service is used.

`KLYK_FIXTURE_COMPACT=1` retains the native fixture's exact 400×600 control content on a desktop at least 1024 pixels wide with enough usable screen area. The primary windows request x=120/540, y=20 in Cocoa coordinates; the selected right window moves to x=560. The receiver's two windows overlap at x=120, leaving the selected source and receiver as separate real drag surfaces. Cocoa can adjust vertical placement to the usable screen area. Reports retain requested y, actual native frame/content bounds, and each window's real `NSScreen.visibleFrame`. Qualification requires visible unique window IDs, fixed horizontal surfaces, aligned observed vertical origins, finite bounds, and complete frame containment in that usable area. Compact geometry adds checks while keeping all 48 required native outcomes. The default full-size desktop layout is unchanged; compact mode still creates visible windows and belongs only on an isolated test Mac or remote runner.

The opt-in [`native-verification.yml`](../.github/workflows/native-verification.yml) provides an isolated remote route on standard GitHub-hosted macOS runners in this public repository. Candidate pushes run only the read-only permission/display probe. A same-repository PR labeled `native-verification`, or an explicit workflow dispatch with `full_suite` enabled, requests the full matrix on both supported MCP majors. The probe checks existing Accessibility and Screen Recording grants, the fixture apps, and sufficient desktop geometry; it repeats with the fresh-installed interpreter before any UI. Missing grants or geometry fail the job and remain incomplete acceptance, without requesting permissions or modifying TCC. Capable runners then run fresh-install doctor, real stdio/schema checks, and the full native and Chromium/Electron suites.

Remote evidence is limited to named fixture JSON reports emitted in checksummed log chunks. Screenshots, application logs, browser/editor profiles, and temporary documents are not uploaded, and no paid artifact storage is used. Download and validate the exact completed reports before release; a capability probe or a passing guarded local protocol check cannot replace the full major acceptance gate.

The checks cover these workflow groups:

1. App discovery, session attachment, and explicit window selection.
2. Screenshots, AX inspection, OCR, and element reads.
3. Semantic clicks, duplicate-label refusal, and explicit disambiguation.
4. Click variants, direct AX actions, hover, and native controls.
5. Unicode typing, command shortcuts, held keys, and clipboard paste.
6. Clipboard restoration and independently observed text values.
7. Menus, context menus, choices, sliders, and scrolling.
8. Save, open, cancel, and absent-dialog refusal.
9. Pixel, grid, template, and bounded visual-wait tools.
10. Window movement, closure, stale targeting, and surviving sibling windows.
11. Native background delivery with measured cursor and focus preservation.
12. Cross-app drag with independently recorded drop contents.
13. Ownership takeover, blocked non-owner input, and dead-owner recovery.
14. Chromium fallback and isolated Electron editing with independent page/file outcomes.
15. Batch interruption, mode refusals, diagnostics, and evidence-qualified verdicts.

Reports contain environment versions, tool calls, timings, payload sizes, independent assertions, and a source fingerprint. Fixture-local counters observe delivered key-down/key-up events; the held-key check requires a matching release rather than inferring it from later text. Absent-dialog refusal also requires unchanged input counters and fixture state. Screenshots, compiled fixtures, temporary documents, and reports stay in ignored `.verification/`. Run the native check with each supported MCP major version in separate environments. A pass covers these fixture cases on the recorded machine, not every control, application, permission configuration, or display setup. Multiple-display hardware needs a separate real-device check. The physical emergency-stop chord also needs a person; automated latch tests do not establish physical shortcut acceptance.

## Release gate

Use a release environment with the project dependencies, `build`, and `twine>=7` (older Twine rejects current Core Metadata 2.5). Update existing tooling with `python -m pip install --upgrade build 'twine>=7'`. The script defaults to `~/.klyk/venv/bin/python`; set `KLYK_RELEASE_PYTHON` to use an isolated release environment.

If the native open check fails, the report retains a read-only diagnostic of the generated fixture host before the original assertion stops the suite. It bounds the host snapshot and raw descendant walk to 400 entries/nodes, clips text to 256 characters, shares a 1.5-second traversal budget with bounded native messaging, and limits actual parent chains to eight links. Raw focus, empty values, field writability, and service PIDs can explain safe refusals that a public snapshot cannot distinguish. It performs no further dialog input, reads no unrelated app tree, and never converts the failure into a pass. This diagnostic belongs only to the isolated native suite and must not run locally while desktop activity is prohibited.

Choose verification by behavior and risk, not the version number. Full native checks on both supported MCP SDKs and the Chrome/Electron suite are required only for major functionality changes: substantial input delivery, targeting, capture, session/ownership, safety, or cross-app workflow changes. Minor setup, documentation, diagnostics, metadata, and schema corrections use targeted checks of the affected behavior; do not rerun unrelated desktop workflows or stop other sessions for them.

Every release still runs the portable regressions, package metadata/privacy checks, fresh-install doctor, and an MCP connection smoke check. Recheck changed behavior after relevant edits. Keep unrelated known failures recorded without representing them as fixed; they do not force a full suite for a minor release. A regression introduced or worsened by the candidate must be resolved before publication.

For a minor release, save a private JSON report with `scope: "minor"`, a nonempty `rationale` describing why the change is minor and what was checked, the current `fingerprint()` from `tests/release_check.py`, `completed: true`, and a nonempty `checks` array of `{name, passed: true}` objects. Include evidence references and remaining limitations. The report is a record of actual verification, not permission to mark unrun checks as passed.

```sh
./release.sh vX.Y.Z --targeted .verification/targeted.json --notes-file /tmp/release-notes.md --dry-run
```

For major functionality changes, supply fresh full-suite evidence:

```sh
python3 tests/release_check.py --live .verification/native-mcp1.json --live .verification/native-mcp2.json --desktop .verification/desktop.json
python3 tests/release_check.py --archives
./release.sh vX.Y.Z --live .verification/native-mcp1.json --live .verification/native-mcp2.json --desktop .verification/desktop.json --notes-file /tmp/release-notes.md --dry-run
```

The release script requires a reviewed, committed candidate on synchronized `main`, passing portable tests, fresh evidence for the selected scope, and clean package archives. Omit `--dry-run` only when publication is authorized. GitHub CI runs portable tests on both MCP major versions; Linux CI does not replace any real Mac checks required by the changed behavior.

Full reports must record supported versions in `environment.mcp`, with native reports for both MCP majors. Each native report must discover and call every tool actually declared in the candidate source, validate every schema returned by the real SDK, and include all independent native acceptance checks. The desktop report must include the independent Chromium and Electron workflow checks. Partial, empty, stale, malformed, or truthy-string assertions are rejected. Privacy checks compare paths without regard to case and reject absolute or parent-traversing archive paths. Tracked Git paths use raw NUL-separated filenames so display escaping cannot hide private directories. These controls validate evidence completeness; the owner still reviews whether recorded assertions describe checks that actually ran.
