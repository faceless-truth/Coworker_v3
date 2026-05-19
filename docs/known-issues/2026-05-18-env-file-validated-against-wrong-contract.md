# PUBLIC_WEBHOOK_BASE_URL was missing because the env file was validated against the wrong contract

- **Status:** OPEN
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** MEDIUM — class issue; other worker-required keys may also be missing
- **Owner:** Elio (unassigned action)

## Finding
The 09fad28 deploy failed at `coworker-subscribe` because
`PUBLIC_WEBHOOK_BASE_URL` was absent from
`/opt/coworker/shared/credentials/coworker.env`. It was added
(`PUBLIC_WEBHOOK_BASE_URL=https://coworker.mcands.com.au`, bare
origin, line 14) and subscribe then ran clean.

## Evidence
`subscribe.py:28` aborts if unset; journal showed the abort.
`subscription_sweep.py:193-195` appends `/webhooks/graph/{firm.slug}`
to the value, so bare origin (no trailing slash, no path) is correct.
`config.py:85` `PUBLIC_WEBHOOK_BASE_URL: str = ""` — defaulted in
Settings, so Settings validation does NOT require it; only the
subscribe worker enforces presence.

## Root cause
The env file was only ever validated against the keys pydantic
`Settings` marks required (5 fields). Worker entrypoints have
ADDITIONAL required keys enforced in their own code, not in Settings.
The env file was never validated against the full set of
worker-required keys.

## Not yet decided / open question
A completeness audit of `coworker.env` against ALL worker entrypoints
(not just Settings) — which other workers have their own
`if not settings.X: abort` guards, and are all those X present in the
env file? Until audited, another worker-required key may be missing
and unhit.

## Out of scope for the finding
The PUBLIC_WEBHOOK_BASE_URL value itself (resolved). This file is
about the validation-gap class.
