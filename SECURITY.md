# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in Email Automation, please **do not**
open a public issue. Instead, report it privately so we can triage and release
a fix before public disclosure.

- Email: security@example.com (replace with the maintainer's real contact)
- Or use GitHub's private vulnerability reporting on the repository's
  **Security → Advisories** tab, if enabled.

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce
- Affected version(s)

We aim to acknowledge reports within 7 days and to ship a fix or mitigation
within 30 days of confirmation, depending on severity.

## Responsible disclosure

We request a reasonable disclosure window (at least 90 days) after a fix is
available before any public write-up.

## Note on secrets

This project never commits `.env`, `opencode.jsonc`, `*.db`, or other local
state. If you find a committed secret, treat it as a critical vulnerability
and report it privately immediately.
