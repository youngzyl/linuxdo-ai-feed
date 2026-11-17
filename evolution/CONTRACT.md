# Lead-owned OpenEvolve integration contract

Mission: use the real pinned OpenEvolve framework to propose measurable, safely reviewed improvements to linuxdo-ai-feed, starting with multimodal batch packing. This is not permission for uncontrolled production self-modification.

## Evolvable surface

A standalone pure `choose_batch(rows)` function returning a list of integer topic IDs. Each synthetic replay row has `id`, `future`, `age`, `images`, `image_bytes`; all rows have already passed immutable production eligibility, image inspection and reservation checks. There is no article text, credential, URL or feedback content in these optimization inputs. The initial implementation reproduces newest-first greedy packing. No changes to provider/model/tier, safety guards, selection windows, evaluation code or relevance rubric are evolvable.

## Fixed evaluator contract

Lead-authored acceptance rules, not mutation targets:
- Returned IDs must be unique integers, belong to the supplied rows, and contain at most eight posts.
- Selected rows total at most eight images and 4 MiB normalized raster bytes. Invalid output fails the entire case; do not repair a candidate's output for scoring.
- If a ready future row exists, include its newest member (smallest age). Do not starve the newest arrival to optimize old backlog.
- Generate a deterministic fixed suite with distinct cases: empty/short/text-only, image-heavy burst, mixed future/history, individually over-cap rows that must be excluded, packing that defeats naive greedy, equal-age tie order and zero-byte images. Include a separate fixed holdout seed with the same rules. Do not expose holdout inputs in mutation prompts.
- Eligibility inside the evaluator means individually feasible: images <= 8 and image_bytes <= 4 MiB, with nonnegative integer counts. The newest-future requirement applies only to these feasible rows. For each coverage denominator use min(8, the number of individually feasible rows in that category); report that this is a slot-count upper bound, not a guarantee all those rows can fit simultaneously. Empty category denominators score one. Combined score is zero for any invalid output or missing newest individually feasible future row; otherwise use 0.7 * mean(selected count / min(8, feasible row count)) + 0.15 * mean(selected image-bearing count / min(8, feasible image-bearing count)) + 0.15 * mean(selected historical count / min(8, feasible historical count)). Require holdout non-regression before recommending promotion. Any gain is an offline candidate, not demonstrated live throughput or preference quality.
- Every candidate executes in an isolated Docker container: network none, no secrets/env/socket/production mounts, read-only root, non-root where available, capability drop ALL, no-new-privileges, bounded memory/CPU/PIDs/time/output. Trusted outer evaluator must not import/exec candidate code on its credential-owning host. The kernel mounts only trusted fixtures/read-only runner or receives code through stdin. Errors/timeouts are invalid scores.

## Real framework and request boundaries

Pin official OpenEvolve commit/version and Python dependencies in an isolated uv environment outside the 256 MiB feed. Use its real controller/population/archive/checkpoints. A custom LLMInterface may adapt official Responses for gpt-6-luna/high/Flex; no Chat Completions assumption or Standard fallback. Keep the project key only on bwgca and read it into memory there. Never put keys in OpenEvolve config serialization, prompts, output, environment exported to candidate code, argv or build context.

Start with two mutation attempts per daily experiment, one worker, max 6,000 output tokens and capped input/prompt bytes per mutation, one whole-run deadline and a nonblocking singleton lock. Set all automatic model retries to zero; persist each run/attempt intent before sending and response handle/model/tier/usage/outcome afterwards. A missing/unknown response must not be automatically paid again, even after cron restart. Daily run IDs are UTC-date scoped; a duplicate admission must read existing receipt rather than rerun. A failed or incomplete run must report blocked/error honestly, not use fake mutations as real API evidence.

Keep a machine-readable report with seed score, best score, holdout metrics, safety result, model/tier/usage, candidate/archive/checkpoint identifiers and review-needed status. The first actual two-attempt run may show no improvement; that is a valid honest outcome. Offline/mock smoke tests are labeled separately.

## Cron workflow

Retain the existing twenty-minute change-triggered watchdog for incident diagnosis. Add a daily bounded experiment job (08:30 Asia/Shanghai, after UTC receipt-day rollover) attached to linuxdo-reader and an OpenEvolve workflow skill, or equivalent non-duplicate cadence. Existing watchdog should load the same safety/recovery knowledge and refer reproducible issues into this candidate workflow, not launch expensive evolution every twenty minutes.

Daily runs invoke the actual pinned runner, retain artifacts and give short evidence-based updates only for an actionable improvement, new failure or recovery. No automatic deployment, merge, credential/tier/rubric edits, historical replay outside the authorized window, quarantine clearing, or writes to linux.do. Any candidate promotion requires independent review against fixed gates and explicit production change approval. Do not ask the user to judge routine low-risk implementation details; escalate only approval-worthy behavior/security/billing changes.
