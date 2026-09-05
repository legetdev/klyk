# Tests

Run the portable regression suite with the installed project dependencies:

```sh
python3 -B -m unittest discover -s tests
```

The portable tests cover validation, batch failures, targeting, emergency-latch enforcement, input cleanup, Unicode and clipboard preservation, ownership, session limits, configuration editing, and publication privacy. Native boundaries are controlled fakes; these tests do not prove real macOS input delivery.

## Real Mac verification

The opt-in native check compiles `Fixture.swift` using `xcrun swiftc`, starts disposable AppKit apps, and drives all 48 tools through the real stdio MCP server:

```sh
python3 -B tests/live_smoke.py --output .verification/native.json
python3 -B tests/desktop_smoke.py --output .verification/desktop.json
```

These checks actively operate the desktop. Run them on a Mac available for testing, with Accessibility and Screen Recording permission for the runner. The native check uses disposable windows, text, files, menus, and dialogs. The desktop check requires Chrome and Visual Studio Code already installed; it creates a local browser page and an isolated editor profile, and refuses to run the editor check if Code is already running. It checks the exact editor process and fixture document before sending input. No paid model or remote test service is used.

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

Reports contain environment versions, tool calls, timings, payload sizes, independent assertions, and a source fingerprint. Screenshots, compiled fixtures, temporary documents, and reports stay in ignored `.verification/`. Run the native check with each supported MCP major version in separate environments. A pass covers these fixture cases on the recorded machine, not every control, application, permission configuration, or display setup. Multiple-display hardware needs a separate real-device check. The physical emergency-stop chord also needs a person; automated latch tests do not establish physical shortcut acceptance.

## Release gate

After the final source edit, rerun both native SDK environments and the desktop check. A report becomes stale when runtime, public documentation, tests, or release controls change.

```sh
python3 tests/release_check.py --live .verification/native.json --desktop .verification/desktop.json
python3 tests/release_check.py --archives
./release.sh vX.Y.Z --live .verification/native.json --desktop .verification/desktop.json --notes-file /tmp/release-notes.md --dry-run
```

The release script requires a reviewed, committed candidate on synchronized `main`, passing portable tests, fresh live evidence, and clean package archives. Omit `--dry-run` only when publication is authorized. GitHub CI runs portable tests on both MCP major versions; Linux CI does not replace the real Mac checks.
