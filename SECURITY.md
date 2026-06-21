# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in looptrip, please report it privately to `[INSERT SECURITY CONTACT]` rather than using the public issue tracker. We appreciate your responsible disclosure and will work with you to address the issue promptly.

## Supported Versions

| Version | Supported          |
|---------|-------------------|
| 0.1.x   | Yes               |

## Attack Surface

looptrip is designed as an offline, read-only detector that operates deterministically on event streams. The library:

- **Does not execute untrusted code.** It parses and analyzes event and handoff-state strings only.
- **Does not make network requests.** All detection is local to the process.
- **Does not modify system state.** looptrip reports findings; it never blocks, kills agents, or triggers actions.

The primary attack surface is untrusted input to the normalized event stream (`Event` objects) and handoff-state strings consumed by the deadlock detector. Malformed or adversarial event data may cause parsing errors or incorrect detection results, but cannot trigger code execution or persistent state changes.

## Dependencies

looptrip's core is stdlib-only. Optional extras (dev testing, OpenTelemetry adapters) are declared in `pyproject.toml`.
