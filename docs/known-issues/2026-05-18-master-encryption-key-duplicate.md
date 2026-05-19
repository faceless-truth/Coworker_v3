# MASTER_ENCRYPTION_KEY appears twice in coworker.env with DIFFERING values

- **Status:** OPEN
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** HIGH — potential undecryptable data
- **Owner:** Elio (unassigned action)

## Finding
`/opt/coworker/shared/credentials/coworker.env` contains two
`MASTER_ENCRYPTION_KEY=` lines (lines 8 and 10). Their values are NOT
byte-identical (confirmed by internal sha256 comparison; values never
printed). Both are 44 bytes (base64-encoded 32-byte keys).

## Evidence
`sudo grep -n MASTER_ENCRYPTION_KEY` shows two assignments. sha256 of
each value computed without display: they differ. All env-loading
mechanisms (systemd `EnvironmentFile=`, shell sourcing,
pydantic-settings `.env`) are last-wins, so the *active* key is line
10 — this is the key the running api has used since v3.0.0.

Detection method (read-only, no values logged):

```bash
v8=$(sudo sed -n '8p' /opt/coworker/shared/credentials/coworker.env | sed 's/^[^=]*=//' | sha256sum | awk '{print $1}')
v10=$(sudo sed -n '10p' /opt/coworker/shared/credentials/coworker.env | sed 's/^[^=]*=//' | sha256sum | awk '{print $1}')
[[ "$v8" == "$v10" ]] && echo identical || echo DIFFER
# → DIFFER
```

## Root cause
Unknown — either (a) a key rotation done by appending line 10 instead
of replacing line 8, (b) an accidental paste/typo, or (c) an
intentional but undocumented backup. The env file is gitignored with
no history; cannot determine which from the repo.

## Which value is currently "active"
Every credential-loading mechanism on the droplet resolves duplicate
`KEY=` lines by **last-wins**:

| Mechanism | Resolution |
|---|---|
| systemd `EnvironmentFile=` (the production api unit) | line 10 |
| Shell `set -a; . file` | line 10 |
| pydantic-settings reading `env_file` (Option C in the fix) | line 10 |
| `systemd-run --property=EnvironmentFile=` | line 10 |

So the running `coworker-api.service` since the v3.0.0 cut on
2026-05-01 has been using the **line 10** value. The line 8 value is
silently shadowed by line 10's redefinition.

## Why this might matter (range of possibilities)
The cryptography implications depend on which scenario is true, and
we cannot tell from on-droplet evidence alone:

1. **Line 8 was never active.** Pasted by accident during initial
   env-file authoring, immediately superseded by line 10 on the same
   edit, never used to encrypt anything. Harmless — line 8 is dead
   data.
2. **Line 8 was active at some point and got rotated by appending
   line 10 instead of replacing line 8.** Any
   `EnvelopeCipher`-encrypted data that landed in the DB during the
   line-8 window is now **silently unrecoverable**: the active key
   (line 10) cannot decrypt it, and the line-8 key, though still
   present in the file, is not what any code loads.
3. **Line 8 is a backup / break-glass key intentionally retained.**
   Unusual placement (production env file with duplicate keys is not
   a backup mechanism), but technically possible if there's an
   undocumented out-of-band convention. Same active-key behaviour as
   #1 — line 10 wins.

**Scenario 2 is the dangerous one.** Detection requires:
- Knowing whether any envelope-encrypted rows exist in production
  (`coworker_test` is irrelevant; the live `coworker` DB is the
  question).
- Attempting decryption of any such rows with **both** keys to see
  which one (if any) works. If the line-8 key decrypts rows that the
  line-10 key cannot, that proves rotation-by-append occurred and
  quantifies the data at risk.

## Recommended next steps (proposed, not actioned)
Elio to decide, separately:

1. **Audit DB for envelope-encrypted data.** Inventory tables /
   columns that store ciphertext (Phase 2 introduced
   `EnvelopeCipher`; later phases may have added more). Pick a
   tenant-scoped column the audit can run against without RLS getting
   in the way.
2. **Probe-decrypt with both keys.** For a small sample of rows, try
   the line-8 key and the line-10 key. Report which (if any) succeeds
   per row.
3. **Decide remediation based on probe results.**
   - All rows decrypt with line 10 → line 8 was never active → remove
     line 8 from the env file. Done.
   - Some rows decrypt only with line 8 → rotation-by-append
     happened. Re-encrypt those rows with the line-10 key using a
     scripted migration (with a fresh `pg_dump` as the safety net),
     then remove line 8. Audit-log the operation.
   - No rows decrypt with either → separate, larger incident; do not
     touch the env file until diagnosed.
4. **Add a Settings-init invariant check** to fail loud at startup if
   any required `SecretStr` field has a duplicate in the env source.
   Cheap belt-and-suspenders against this exact class of issue
   recurring.

## Not yet decided / open question
Was line 8 ever the active key? If yes, any `EnvelopeCipher`-wrapped
data written during the line-8 window is now undecryptable (the
wrapping key is line 8; everything now resolves to line 10).
Resolution requires the procedure in **Recommended next steps**
above. NOT actioned — Elio's separate decision with its own audit
trail.

## Out of scope for the finding
This is independent of the alembic env-loading fix and the rollback
work. Do not "fix" by editing the env file — that is the decision
this finding exists to inform, not pre-empt.
