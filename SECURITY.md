# Security Policy

## Supported versions

Klyk is a portfolio project with a slow maintenance cadence. Ordinary bug reports are handled as time permits and are not actively triaged; security reports are read and triaged through this policy. Fixes and written dispositions ship on a best-effort timeline. The current `main` branch is the only supported version.

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
- **Agent → klyk:** the agent (Claude / Cursor / Cline / etc.) drives klyk via the MCP protocol over stdio. Klyk executes the requested intent only while that environment's saved access switch is On, subject to target-window bounds, duplicate-label rejection, the single-owner token and emergency latch. Confirmation guidance and `confirm_destructive` are agent-cooperative, not user-consent enforcement. Run klyk only with agents you trust to act on your behalf.
- **Klyk → outside world:** klyk makes one optional once-daily HTTPS request for public PyPI package metadata. The check sends no captured screen data, OCR, or tool results, but normal network metadata such as the IP address is visible to PyPI and the network path. Tool results delivered to the calling agent/client may be transmitted onward to its model provider under that client's settings. Set `KLYK_UPDATE_CHECK=0` to disable the check and klyk makes no network calls itself.

## Per-environment switches

The separate native menu-bar controls persist access under `~/.klyk/connections.json`; they perform no computer-use reads or actions. Missing, malformed, overly broad permissions, links or special files fail closed. Every computer tool checks its environment before queuing work; queued workers, later native stages and the final tool reply retain the original generation. Turning Off cancels pending requests without disconnecting MCP, and turning On cannot revive earlier work. Discovery and ping remain available while Off.

An operation already submitted to macOS may finish. Cleanup may release previously held input or restore a borrowed clipboard. Data already returned to a client cannot be withdrawn. Off does not close target apps or clear the physical emergency latch. Old versions must be upgraded and restarted once; they do not honor this policy.

`KLYK_CLIENT` tags and conservative legacy parent identification group trusted local clients. They are not cryptographic identity: a process running as the same user can change the private file, choose a tag or bypass Klyk entirely. No MCP tool changes these switches, but an agent with separate shell access under the same account can edit them. The switches are a durable owner convenience and runtime refusal inside the existing trusted-local model, not an OS sandbox or a guarantee against prompt injection.

## Prompt injection & the confused-deputy risk

klyk executes whatever the connected agent tells it to. If that agent also ingests untrusted content — a web page, an email, a PDF, a chat message — a malicious instruction hidden in that content can be turned into real clicks and keystrokes on your Mac. This is the classic *confused-deputy* problem, and the "agent that reads the web **and** drives the machine" configuration is the highest-risk way to run klyk.

klyk's consent guidance is **agent-cooperative, not enforced**: money/destructive guidance, `confirm_destructive`, and any bounds override are not proof of user approval, and a valid in-window click can still be destructive. Code also enforces target-window bounds, duplicate-label rejection, one active control owner, and the `Cmd+Shift+Esc` emergency latch. Each running server blocks new input after observing the chord and releases held keys and buttons. Holding the chord does not toggle it repeatedly. Only a second physical chord clears that server's latch; the `resume` tool only reports status. The latch is process-local: restarting or reconnecting starts a new server with an inactive latch. It is not a persistent pause for future servers or a security boundary against other software running as the same user.

Mitigations are operational, not technical: run klyk only with agents and workflows you trust, keep untrusted-content reading and machine control in separate sessions where you can, and supervise anything consequential.

## What's scrubbed

Stderr from apps launched by klyk is run through credential scrubbers at capture time before being stored in the in-session log buffer. The scrubbed patterns:

- Passwords, API keys, access/refresh/session tokens, client secrets, AWS access/secret keys, and private-key fields in common `key=value` and quoted forms (key visible, value replaced with `***`)
- Quoted JSON credential fields such as `"password": "…"`, including spaces and escaped quotes in the value
- `Authorization: Bearer …` HTTP headers
- AWS access key IDs (`AKIA*`, `ASIA*`, …)
- JWTs (`eyJ…`.`…`.`…`)
- Single-quoted credential values, Basic/Digest Authorization headers, and Cookie/Set-Cookie headers

Captured log records are limited to 8 KiB each and 500 records per channel. Oversized records are omitted entirely so a truncated credential's tail is not exposed. Persistent diagnostics use a dedicated Klyk logger, escape control characters, and omit tracebacks, request values, unrecognized tool names, and arbitrary argument keys. MCP protocol debug messages and tool exception payloads are not written to that log. Failed connection messages report fixed diagnostic categories instead of replaying server stderr.

This is defense-in-depth — agents shouldn't be trusted to filter credentials downstream, and a misbehaving app that prints secrets to stderr shouldn't infect the rest of the trust chain. It is **best-effort; it cannot catch every credential format — not a guarantee.** Do not rely on it as your only safeguard.

## What's deliberately not scrubbed

- Screenshots and OCR text returned to the agent: the agent asked for the pixels, so it gets them. Don't run klyk on screens with content you can't show the agent.
- Window screenshots capture only the selected window, including when covered. Compatibility fallbacks retain that window ID; failure never widens to the desktop. Explicit display or region capture includes whatever is visible in that requested area.
- AX labels and values: same rationale — the agent asked.

## Local files and resource limits

The shell client stores up to 20 recent screenshots in `~/.klyk/captures`, an owner-only directory. New screenshot files, explicit screenshot exports, the ownership token, and diagnostic logs are created with owner-only permissions. Existing diagnostic rotations are restricted when logging starts. These are filesystem permissions, not encryption; software running as the same user can still read them. Old log contents are not erased automatically and may contain request data recorded by earlier versions.

Klyk refuses symbolic links, hard links, and special files at its private-file write boundaries. An unsafe or unwritable log path disables file logging without preventing the MCP connection. A failed screenshot export keeps the inline image and returns `save_error`. User-selected parent directories remain under the user's control; Klyk is not a filesystem sandbox for an untrusted agent.

PNG decoding and OCR validate a 32 MiB encoded-data limit, at most 8192 pixels per side, and at most 16 million pixels before native allocation. Template extraction and matching reject an estimated working set above 512 MiB before decoding or allocating image arrays; use a smaller screenshot or search region when refused. Each session retains at most 50 templates and 8 MiB of encoded template data, with at most 64 sessions. The shell client's response buffer is limited to 64 MiB. These limits reject malformed or excessive payloads; they do not make an untrusted MCP agent safe to run.

Generated MCP launch commands use Python's safe-path flag to exclude the current working directory from module lookup. Apple command-line helpers use fixed system paths so inherited `PATH` cannot substitute a workspace executable. Klyk does not automatically discover or load `.env` files; configure environment values explicitly in the launcher or MCP client. This does not isolate Python from a deliberately configured import path or other software running as the same user.

Control handoffs refuse while another server is delivering or releasing input. App termination and CLI restart check the process start identity before signaling, so a reused PID does not authorize terminating its replacement. Numeric requests reject NaN and infinity before execution. Verdict and grading capture retain the selected window and refuse missing or reassigned windows instead of selecting another document.

## Publication

GitHub Actions builds and tests packages without PyPI identity-token permission. A separate job receives only the built distributions and the publishing permission. Manual retries require an existing stable GitHub release whose tag matches the package version. Official actions are pinned to immutable commits; no persistent PyPI token is stored in the repository.

## Out of scope

- Vulnerabilities that require the user to already be running malware or to have granted system-wide screen-control to a hostile process. Klyk is downstream of those exploits.
- Issues in third-party MCP clients (Claude Code, Cursor, etc.). Report those upstream.
- Bugs in macOS frameworks. Report those to Apple.

## Acknowledgements

Reporters who follow coordinated disclosure get a credit in the release notes if they want one.
