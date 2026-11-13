# Luna Flex image screening

## Authorized scope

Use OpenAI Platform `gpt-6-luna`, `reasoning.effort=high`, `service_tier=flex`, with actual OP raster images. No Standard/provider fallback. Enable current/future arrivals and a fixed seven-day historical window of still-unjudged posts; preserve existing verdicts, manual feedback and bookmarks. The effective activation and backfill timestamps must be explicit and persisted.

## Release implementation

The backend source is copied from reviewed archive SHA-256 `a3888df16102ec0ad0b2ef110b94eecec7e7811c9257aa532d116c242866c001`; manifest SHA-256 `4367feb13e9949511b09ca7e5e1c8fdedc530b22992acdbfc48ca2eb8f609bf8`. Frontend assets are intentionally unchanged in this commit.

- Single process writer, locked CAS verdict admission, durable state and receipts, fail-stop after ambiguous atomic persistence.
- Bound and pin public DNS destinations, verify hostname TLS, validate redirects; exact image hosts include linux.do, cdn.linux.do, cdn.ldstatic.com and cdn3.ldstatic.com. Spawned bounded decoder, raster validation, image count/byte/pixel/time limits.
- Missing authoritative OP content remains uninspected, not a text-only classification. Detail hydration and image-download retries are distinct; absent metadata does not consume download failures. Fair detail rotation advances only attempted work.
- Per-request remaining image budget causes deferral, not false quarantine. Genuine incomplete/oversize images remain observable, never silently classified without images.
- `sv1` historical Batch fingerprints are preserved; Flex additionally fences image metadata changes. Unknown paid responses stop automatic retry and require reconciliation.

## Cancellation reconciliation

The original run `luna-day-1794512882`, provider Batch `batch_6af618167de881908896521940316f52`, was cancelled. Provider GET confirmed terminal `cancelled`, 71 total, zero completed, no output file. Its error file contained exactly the 71 outstanding owner group IDs, all `batch_cancelled`. The owner run was finished as cancelled. Only that reviewed explicit group list is released; the realtime receipt and already-applied verdicts remain protected. The private declaration resides on bwgca; no credentials or article bodies belong in Git.

## Executed checks before final image cutover

- 169 final offline tests passed in the implementation workspace.
- Earlier same-code service plus spawned decoder under a 256 MiB cgroup: peak 148,455,424 bytes, no OOM, health HTTP 200 before/after. This is a synthetic 1,000-topic test, not proof of unlimited store growth.
- One real complete image-bearing RSS post was fetched and sent as raster bytes in an authorized test-only request. Provider returned completed, actual `gpt-6-luna` and `flex`, 2,235 input and 172 output tokens. No verdict was applied. This exercised the build immediately before a fairness-only collection change, not the final deployment image.
- Independent static reviews identified and led to fixes for missing image-source hosts, per-request byte budget handling and hydration starvation. The final hydration/fairness delta was reviewed by the lead.

## Production cutover

Deployed image `sha256:389533d52e4d95b1d30db57e0c864b0b5f850e8b91bd5db873bb25244f3cccb9` with config SHA-256 `2a22482290f6083a6031599e08bcf7bca9563817f46e9c9e7458f2d2a4c4c78a`. Both activation and historical T0 are `1794557062`; the historical start is `1793952262`. The scheduler continues every 15 minutes, prioritizing future arrivals while draining the authorized seven-day unjudged window.

The first production image applied six Flex verdicts, including three picked; state showed five historical topics and one future arrival. The final health-accounting image's startup cycle applied eight more verdicts, two picked. Public health was independently read back as `status=ok`, `filter.ok=true`, `service_tier=flex`, one successful batch, zero failed batches, no last error and `attention.needed=false`. Usage for that cycle was 4,745 input and 1,540 output tokens, including 990 reasoning tokens. Public per-topic GET also confirmed prior image-bearing verdicts remained available.

All 32 bookmarks, six feedback records, the feedback journal and original Luna receipts were preserved by before/after hash comparisons. Background seven-day processing is enabled, not completed. Uninspected or failed images are deferred rather than silently classified as text-only; hydration backlog is reported separately from provider failure. Direct detail reads may receive HTTP 429 and use the existing configured fallback. No Standard/provider fallback is enabled.

The final source suite passed 171 tests; the final image passed 15 targeted tests. Lead worktree checks independently passed 58 integration/security tests before cutover and 17 hydration/health regression tests after the final delta.

Rollback uses the compatible candidate image with both filters disabled and the same live state. Never restore an old state snapshot over newer bookmarks or replies, and never use an older image that drops Luna/Flex fields.
