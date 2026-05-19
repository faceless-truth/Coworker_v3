# deploy.sh has 14 hardcoded production references and no test seam

- **Status:** OPEN
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** HIGH — root cause of the entire 2-day failure sequence; this is the headline
- **Owner:** Elio (unassigned action)

## Finding
The committed `backend/scripts/deploy.sh` LOCAL path has 14 hardcoded
production references (production symlink path, releases dir prefix,
DB name, `/etc/systemd/system/`, real `daemon-reload`, real unit-name
arrays, etc. — none parameterised). There is no dry-run, no isolation
seam. The script CANNOT be exercised anywhere except against real
production.

## Evidence
Enumerated during the Step-4 proof-rig attempt: items include L31
`RELEASE_DIR="/opt/coworker/releases/$RELEASE"` (prefix literal), L192
`PREV_RELEASE=$(readlink -f /opt/coworker/current)`, L194
`ln -sfn "$RELEASE_DIR" /opt/coworker/current`, L110/131
`-d coworker`, L181-184 install to `/etc/systemd/system/`, L189
`daemon-reload`, L38-55 unit-name arrays. Input-only redirection of
the unmodified script was proven impossible (no
`${CURRENT_SYMLINK:-...}` style seams).

## Root cause
The script was written to do one thing against one host and never
given a test/dry-run mode. Every defect (alembic env-loading, missing
PUBLIC_WEBHOOK_BASE_URL, the §3 rollback gap) therefore could only be
discovered in production — which is exactly what happened, five
times.

## Not yet decided / open question
The fix. This becomes its own deliberate, discovery-first task (the
NEXT task after this findings log). Candidate direction:
parameterise the production references behind env-overrides that
DEFAULT to today's exact literals (so production behaviour is
unchanged) enabling a throwaway-target dry-run. NOT to be designed
here — flagged as the priority task.

## Out of scope for the finding
Implementing the fix. This file establishes WHY it is the
highest-leverage next work.
