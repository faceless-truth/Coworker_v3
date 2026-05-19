# 09fad28 deploy left production on a half-deployed release, undetected for ~4 hours

- **Status:** OPEN
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** HIGH — silent production-state divergence (now mitigated by 3051d14)
- **Owner:** Elio (unassigned action)

## Finding
The 09fad28 deploy (2026-05-18 ~07:28 UTC) did NOT fail
pre-symlink-swap as believed throughout the incident. It swapped
`/opt/coworker/current` to `09fad28`, started api+worker successfully
on the new code, then failed at the §3 `coworker-subscribe.service`
start. The old deploy.sh had no §3 rollback, so it exited leaving
production running on `09fad28`. This was assumed to be `v3.0.0` by
the operator and the assisting model for ~4 hours.

## Evidence
Discovered only at the 3051d14 pre-flight:
`readlink -f /opt/coworker/current` → `09fad283...` (not `v3.0.0`).
Corroborated: `readlink -f /proc/<api_pid>/cwd` →
`/opt/coworker/releases/09fad283.../backend`; api/worker process start
time `Mon 2026-05-18 07:29:00/07:29:01` (= the 09fad28 deploy's
restart). Symlink, cwd, running code, start time all coherently at
09fad28 — production was stable, just NOT where everyone believed.

## Root cause
The §3 unit-start coverage gap in deploy.sh: under `set -euo pipefail`
a non-zero systemctl in §3 exited before any rollback call-site,
leaving the swapped symlink in place with no operator signal.
Compounded by status assumptions never being verified against
`readlink` until pre-flight.

## Not yet decided / open question
None on the deploy mechanism — 3051d14 closes the §3 gap (prints a
paste-ready manual rollback block on §3 failure instead of silent
exit). OPEN only as a process note: "verify production state against
the system (readlink/cwd/start-time), never carry forward an
assumption about which release is live." Keep OPEN until that check
is institutionalised in the pre-flight runbook.

## Out of scope for the finding
The fix itself (3051d14, shipped). This file is the incident record
and the justification for that fix.
