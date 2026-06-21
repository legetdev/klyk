# Security Policy

## Supported versions

Klyk is a portfolio project with a slow review cadence. Security reports are read and triaged, but fixes ship on a best-effort timeline. The current `main` branch is the only supported version.

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue. Instead:

1. Email the maintainer at the address in the GitHub profile, or
2. Open a private GitHub Security Advisory at <https://github.com/legetdev/klyk/security/advisories>.

Include:
- A clear description of the issue and its impact
- The version / commit you found it on
- A minimal reproducer if possible

You should expect an acknowledgement within ~7 days. A fix or written disposition follows on a best-effort basis. Coordinated disclosure preferred — please give the project a reasonable window to fix before publishing details.

## Trust model

Klyk runs entirely on the user's local Mac, with permissions granted by the user via macOS System Settings. The trust boundaries are:

- **User → klyk:** the user grants Accessibility and Screen Recording via macOS Settings. `klyk doctor` reports the current state.
- **Agent → klyk:** the agent (Claude / Cursor / Cline / etc.) drives klyk via the MCP protocol over stdio. Whatever the agent asks klyk to do, klyk does — within the bounds-check, safety-guard, and confirm-destructive flags. Run klyk only with agents you trust to act on your behalf.
- **Klyk → outside world:** none. Klyk makes zero network calls. Captured screen content, OCR results, and tool responses never leave the local machine via klyk.

## Prompt injection & the confused-deputy risk

klyk executes whatever the connected agent tells it to. If that agent also ingests untrusted content — a web page, an email, a PDF, a chat message — a malicious instruction hidden in that content can be turned into real clicks and keystrokes on your Mac. This is the classic *confused-deputy* problem, and the "agent that reads the web **and** drives the machine" configuration is the highest-risk way to run klyk.

klyk's safeguards are **agent-cooperative, not enforced**: the money/destructive guidance in tool descriptions is text the agent can ignore, and the only guard enforced in code is the geometric window-bounds check (a click must land inside the target app's window — it does **not** stop a destructive click *inside* it). The `Cmd+Shift+Esc` emergency stop is a hard latch: it blocks **all** input until the user presses the chord again to clear it — the agent **cannot** resume it (the `resume` tool only reports status).

Mitigations are operational, not technical: run klyk only with agents and workflows you trust, keep untrusted-content reading and machine control in separate sessions where you can, and supervise anything consequential.

## What's scrubbed

Stderr from apps launched by klyk is run through credential scrubbers at capture time before being stored in the in-session log buffer. The scrubbed patterns:

- `password=…`, `secret=…`, `token=…`, `api_key=…`, `access_key=…`, `auth=…`, `bearer=…` (key visible, value replaced with `***`)
- `Authorization: Bearer …` HTTP headers
- AWS access key IDs (`AKIA*`, `ASIA*`, …)
- JWTs (`eyJ…`.`…`.`…`)

This is defense-in-depth — agents shouldn't be trusted to filter credentials downstream, and a misbehaving app that prints secrets to stderr shouldn't infect the rest of the trust chain. It is **best-effort; it cannot catch every credential format — not a guarantee.** Do not rely on it as your only safeguard.

## What's deliberately not scrubbed

- Screenshots and OCR text returned to the agent: the agent asked for the pixels, so it gets them. Don't run klyk on screens with content you can't show the agent.
- AX labels and values: same rationale — the agent asked.

## Out of scope

- Vulnerabilities that require the user to already be running malware or to have granted system-wide screen-control to a hostile process. Klyk is downstream of those exploits.
- Issues in third-party MCP clients (Claude Code, Cursor, etc.). Report those upstream.
- Bugs in macOS frameworks. Report those to Apple.

## Acknowledgements

Reporters who follow coordinated disclosure get a credit in the release notes if they want one.
