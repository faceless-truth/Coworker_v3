# Sweeps report firms=1 but installations=0 / users=0 — expected pre-onboarding, or a gap?

- **Status:** OPEN
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** LOW-UNKNOWN — likely correct lifecycle state; needs confirmation
- **Owner:** Elio (unassigned action)

## Finding
Post-deploy, `coworker-subscribe` logged
`subscription sweep done firms=1 users=0 actions={}` and
`coworker-scheduler` logged
`scheduler sweep done firms=1 installations=0 fired=0 actions={}`.
Both found the firm, found nothing to act on, did nothing. Exited
clean (0).

## Evidence
Journal lines from `coworker-subscribe` (2026-05-18 07:50) and
`coworker-scheduler` (2026-05-18 11:55). Both `firms=1`, zero
downstream entities.

## Root cause
Most likely the CORRECT state: v3 is pre-cutover, in shadow mode,
parallel to v2.x. Users/installations are provisioned during
onboarding (a later phase), not at infra deploy. Alternative: they
should already exist and a query-scope/migration issue hides them.

## Not yet decided / open question
At this lifecycle stage (post-infra-deploy, pre-onboarding, shadow
mode), is zero installations/users the EXPECTED state per the
architecture doc's onboarding phase, or should they be present? A
~5-minute check against the architecture doc onboarding sequence
when fresh. Do NOT investigate as a bug until shown to be one.

## Out of scope for the finding
Treating this as a defect prematurely.
