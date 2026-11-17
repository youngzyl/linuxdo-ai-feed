# Flex uncertain-dispatch recovery

## Incident and evidence

The persisted future Flex state was `unknown`, not `auth_disabled`. The last retained successful request was from 2026-11-15 07:25:47 UTC. Eight IDs remained pending: 3179831, 3179830, 3179829, 3179823, 3138012, 3138008, 3137989, 3137986. The previous implementation retained neither the triggering error nor a provider response ID for this attempt. Its original HTTP/transport/schema outcome cannot be established retrospectively. Previous successful usage is not evidence about this attempt.

The fail-closed mechanism correctly prevented automatic duplicate paid submission, but left the entire incremental worker blocked with insufficient reconciliation diagnostics. Fetch challenges were independent and later fetch cycles succeeded. The active-target image was stale, not evidence that the container used the legacy provider.

## Repair

- Added an owner-authenticated recovery operation with dispatch-lock then Store-lock ordering, exact pending-ID and attempt-digest CAS, in-flight refusal and idempotent recovery receipts.
- Quarantined the exact uncertain eight without resending them. Exclusions survive historical group release. Their outcomes remain unresolved; no verdict was invented.
- Persisted request nonce, start, source/image versions, sanitized phase/error/HTTP status and bounded response ID/status/model/tier/usage before validation. Authentication and uncertain-outcome attention are distinct.
- Deployed image `sha256:940885cf239a303d333a4a29577e733421befa2e8738369407d18d30fb51d1aa`, source commit `d1dbdd1c7d49573e7ec0e24fe1ff07dc99d9b2e0`.
- Preserved configuration hash `2a22482290f6083a6031599e08bcf7bca9563817f46e9c9e7458f2d2a4c4c78a`, activation/backfill T0 `1794557062`, resource limits, key mounts, bookmarks and feedback. No direct live-JSON edits or counter resets were used.

## Executed checks

- Isolated image: 59 targeted recovery/HTTP/diagnostic/health tests passed.
- Lead worktree: 47 recovery/Flex/integration tests and 37 diagnostic/health tests passed; diff check passed.
- An earlier broad 469-test run had one failure and one error, both reproduced on the unchanged baseline. It is not reported as an all-green suite.
- After recovery, the next scheduled cycle made a real completed `gpt-6-luna` request with actual tier `flex`: eight verdicts applied, one picked; 10,582 input and 1,462 output tokens, including 930 reasoning tokens.
- The new verdict IDs do not overlap the quarantined eight. Persisted image metadata totals six images across three newly judged posts; no separate provider-side image-count receipt was claimed.
- Public health independently read back `status=ok`, fetch/filter true, no last error and no attention. The normal successful cycle cleared inherited filter errors. Scheduler continues every 15 minutes.
- Hash comparisons preserved 32 bookmarks, six feedback records, feedback journal and original Luna receipts.

## Remaining limitations

The original uncertain eight are quarantined, not screened or resolved. Backfill continues; a successful cycle is not proof the complete historical window is drained. Direct detail fetches can still encounter 403/429 and use the existing fallback. Future unknown outcomes still fail closed and require reconciliation rather than automatic paid retries.
