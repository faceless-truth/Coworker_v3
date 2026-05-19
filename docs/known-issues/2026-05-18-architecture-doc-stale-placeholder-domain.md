# Architecture doc uses stale placeholder v3.mcs-coworker.com.au, conflicts with live coworker.mcands.com.au

- **Status:** OPEN
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** LOW — latent trap for anyone treating the arch doc as authoritative on hostnames
- **Owner:** Elio (unassigned action)

## Finding
`MCS_CoWorker_v3_Architecture.md` uses `v3.mcs-coworker.com.au`
throughout. The live production domain is `coworker.mcands.com.au`
(deploy doc "Web hostname", the Xero XPM production redirect URI,
and the live `/health` endpoint all confirm this). The architecture
doc's value is a never-updated placeholder.

## Evidence
`grep` of the architecture doc shows `v3.mcs-coworker.com.au`;
`ENVIRONMENT_AND_DEPLOY.md` and the running system use
`coworker.mcands.com.au`. `curl https://coworker.mcands.com.au/health`
returns 200 (the real host).

## Root cause
Placeholder domain written early, never reconciled when the real
domain was confirmed. Same drift class as the rollback-semantics doc
overclaim that was caught and fixed in 3051d14.

## Not yet decided / open question
Whether to fix in-place (global replace in the architecture doc) or
add a correction note. The architecture doc is maintained outside
the repo's normal commit flow (Elio updates it separately), so the
fix path is an Elio decision, not a code change. Flagged so no
future reader treats the arch doc as authoritative on hostnames.

## Out of scope for the finding
Editing the architecture doc in this task.
