# OpenEvolve daily runner — ledger fix

Cron is not activated by this bundle; the parent owns the activation decision. On bwgca, the ready daily command is:

`cd /home/youngzyl/linuxdo-evolve/daily-release-prompt && OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 /home/youngzyl/linuxdo-evolve/.venv/bin/python daily.py --credential-file /home/youngzyl/.config/linuxdo-ai/openai-api-key --deadline 1100`

The parent-selected 08:30 CST schedule is 00:30 UTC, after the UTC receipt day rolls over. Do not override the date or replay previous attempts.

The file path is not the key. The isolated Responses service reads the private key and workers use peer-authenticated IPC; SDK retries are disabled. Daily UTC receipts are in `/home/youngzyl/linuxdo-evolve/experiment-receipts`, output is in `/home/youngzyl/linuxdo-evolve/daily-runs/YYYY-MM-DD`; never change directories to evade the two-intent daily limit. A new experiment gets a durable per-day random nonce and is not a retry of an old unknown mutation. No automatic promotion.

The owner has explicitly abandoned both 2026-11-17 unknown attempts without altering their outcomes or claiming either was uncharged. A full pre-change snapshot is stored as `owner-snapshot-ac53fb7170e487d54b838a2f338a2e49661f92cb579dac209b3f4f7ef35200ce.json`; its SHA-256 is the hash in its filename. This is a hash-verifiable snapshot, not a cryptographic signature. The updated receipt is `2026-11-17.json` SHA-256 `c41f9c7d0c6049037fd908f5c9ef46c656549ec92d5c40c8dd357847378a6ec1`. The existing 2026-11-17 UTC cap is exhausted, regardless of local CST date; running again on that UTC day returns the old failed report with zero calls. Unknown attempts are never silently retried.

The new adapter can journal `capacity_deferred` only when a cached, fully-consumed bounded HTTP 429 JSON error proves `flex_unavailable` code or `resource_unavailable` type, with quota precedence. Unknown/partial responses remain unknown. Deferred attempts stop dispatch for that day; a subsequent UTC day can run normally. An SDK 429 fixture verified this with a real RateLimitError object but no provider request.

An explicitly authorized independent operator canary on 2026-11-17 made one successful real provider call, gpt-6-luna/Flex. Its one response was reused for the second iteration; the framework usage log double-counted it, so use the durable receipt for actual usage. Both generated candidate evaluations were invalid (fitness zero); the selected best remained the original valid seed. This proves provider/controller/scorer integration, not an improvement. The main report's `safety_valid` pertains to seed/best re-evaluations, not every generated program. Shared-journal daily admission scans unresolved canaries as well as daily receipts. A canary is an operator-only explicit budget, never created by daily cron.

The successful canary receipt is `/home/youngzyl/linuxdo-evolve/experiment-receipts/canary-2026-11-17-c253fd94e5f27dd2f81b720405b8f802.json`; its report and checkpoints are in `/home/youngzyl/linuxdo-evolve/operator-canary-runs/canary-2026-11-17-c253fd94e5f27dd2f81b720405b8f802/`. A deferred capacity run will have status `capacity_deferred` in its own report and journal; the successful canary was not deferred.

For an unresolved historical daily or canary receipt only, an operator can inspect the receipt, obtain its exact SHA-256, and explicitly invoke `owner_abandon_all.py --run-id RUN_ID --expected-sha256 RECEIPT_SHA256 --confirm ABANDON_DO_NOT_RETRY`. This records a hash-verifiable original snapshot and disposition under both locks; it never contacts the provider, does not imply no charge, and is never run by cron. A historical unknown without a nonce receives a fresh audit nonce; an existing valid nonce is preserved. Global quota/auth errors are not capacity-deferred or automatically abandoned.

Pinned OpenEvolve commit `a7950ffc0150f1e7282a39d1ffac57d675dc5551`, codeload archive SHA-256 `ba1537258fd4b156babb9ba13f93c50f3a7a26719b975e9417337bd4a85fc3b2`. The earlier real offline Docker scorer baseline passed, seed/best training 0.8805555555555554, holdout 0.8666666666666666; static mutations only.
