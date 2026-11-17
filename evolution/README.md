# Bounded OpenEvolve experiments

This lane runs the actual OpenEvolve controller against a pure multimodal batch-packing function. It does not change the production feed automatically.

## Installed deployment

- Backend release: `/home/youngzyl/linuxdo-evolve/daily-release-prompt` on bwgca.
- Interpreter: `/home/youngzyl/linuxdo-evolve/.venv/bin/python`.
- Framework source: `a7950ffc0150f1e7282a39d1ffac57d675dc5551`; source archive SHA-256 `ba1537258fd4b156babb9ba13f93c50f3a7a26719b975e9417337bd4a85fc3b2`.
- Controller SHA-256: `31fe6dd20ca63c13235a3e7c257ac3016e2bb225c54ca3988bcc9bedf62f8643`.
- Shared receipts: `/home/youngzyl/linuxdo-evolve/experiment-receipts`; daily reports: `/home/youngzyl/linuxdo-evolve/daily-runs/YYYY-MM-DD/live-report.json`.
- Cron job `f6d0614d7545` is enabled at **08:30 Asia/Shanghai**, after UTC receipt-day rollover. The twenty-minute watchdog remains separate.

See `controller-README.md` for the exact backend command, existing operator-canary handles and audit recovery procedure. Run through Hermes terminal tools: the host cron account and tool backend do not share SSH authentication. Do not copy keys to bridge this difference.

## Fixed boundaries

`CONTRACT.md`, `fixtures.py` and `scorer.py` define immutable training/holdout evaluation. Candidate code executes only in the pinned Docker sandbox, without network, production mounts, credentials or Docker socket. The outer scorer validates IDs and computes fitness; candidate-supplied scores are not trusted. Resource limits and verified container cleanup fail closed.

The trusted Responses service owns the backend-only key and communicates with framework workers through peer-authenticated IPC. Worker configuration is credential-free. The configured generator is `gpt-6-luna`, high reasoning, actual Flex, no automatic SDK retries or Standard fallback. Each daily run has a canonical journal and at most two request intents. A complete, proven capacity 429 defers that day rather than blocking all future days; ambiguous attempts require explicit CAS-audited owner abandonment and are never silently replayed.

No automatic merge, deployment, relevance-rubric edit, credential/tier change or production data mutation is permitted. A promising candidate must improve training fitness without regressing holdout and pass independent review before production approval.

## Real execution evidence

An independently authorized operator canary completed through the real provider/controller/evaluator: response `resp_00a20823ec18ff96016afc8d73dc0887d0baf91295efbc44a0`, one actual Flex call, 747 input and 4,803 output tokens. Two framework iterations reused that single generated response. Durable receipts, not framework iteration counters, are authoritative for provider calls and usage.

The mutation failed fixed validity checks; the valid seed remained best. Seed/best training fitness was `0.8805555555555554`, holdout `0.8666666666666666`. This proves live integration, **not an optimization gain**. The final prompt now explicitly states the newest-future and packing constraints; real PromptSampler plus IPC tests confirmed delivery of those constraints without another paid call. Future runs retain both rejected mutations and the selected baseline.

Final prompt/ledger focused remote suites ran 44 tests: 43 passed, one skipped because dropping credentials requires root. Separate real Docker isolation tests were exercised on bwgca; local absence of Docker was not counted as a passing isolation test. Earlier unknown attempts remain unknown and explicitly abandoned; no zero-cost or successful-generation claim is made for them.
