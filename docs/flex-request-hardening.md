# Luna Flex request mechanism hardening

## Implemented and deployed behavior

The official Responses request keeps `gpt-6-luna`, high reasoning and `service_tier=flex`, with actual OP images. It now requests strict JSON Schema verdict output with exact group length and rejects unknown IDs/schema mismatches during admission. The output cap is 8,192 tokens and the transport timeout is 900 seconds, matching the longer latency envelope recommended for Flex. There is no Standard/provider fallback or automatic transport retry.

Error handling separates full, recognized resource-limit 429 responses (backoff), quota exhaustion (billing-disabled), authentication rejection (auth-disabled), deterministic request configuration rejection and unknown execution outcomes. Interrupted or unrecognized 429 responses remain fail-closed. Bounded diagnostics preserve observed body completeness, length/hash, JSON shape and restricted code/type fields without retaining raw provider messages, article bodies or credentials.

With the explicitly enabled `completed_invalid_policy=quarantine`, a known completed/incomplete Luna Flex response carrying a valid provider response ID but no usable verdict creates a durable unresolved receipt, quarantines its exact IDs and allows unrelated work to continue. Unknown transport outcomes are not retried or automatically treated as completed. Quarantine overrides historical Batch release exclusions.

## Actual deployment

- Image: `sha256:98a2725b1c96de54a85bdb5061d47daab7b98c1a0bd8e80746726163c222e098`
- Backend code commit: `5589466d6cfc561f8940bf7c10b1813c3b45b92a`
- Config SHA-256: `3ea28950810b270e23414268b254748758d4e7d2a7d0ee4a0b0e4ee2e19705b0`
- Original activation and historical T0 remain `1794557062`.

The first policy rollout received 429 without usable diagnostics; it did not establish the original error code or schema compatibility. Those eight requests were not replayed. A sanitized operator diagnostic archive was fsynced before authenticated CAS recovery; its SHA-256 is `e70dcd1be52b525933823f776ba565bd1c6d98e99248961b2b174bcfc67bf118`. That archive is separately correlated to the recovery receipt, not claimed to be embedded in the same atomic transaction.

Sixteen IDs are now quarantined across the two uncertain attempts. Their original outcomes are still unresolved. No fictitious verdicts or free-request assumptions were used.

## Real execution and readback

The subsequent real structured-output request completed, passed validation and applied eight verdicts, including four picked. Actual provider model/tier were `gpt-6-luna` / `flex`; usage was 13,065 input and 1,846 output tokens, including 1,278 reasoning tokens. None of the sixteen quarantined IDs appeared in the attempt. Persisted metadata totals seven images across four of these posts; this is not a separate provider-side image-count receipt.

Lead independently read public health as `status=ok`, fetch/filter true, no last error and no attention, and checked the exact running image with zero restarts. Bookmarks (32), feedback (6), feedback journal and original Luna receipts retained matching hashes. Backfill remains ongoing, not completed.

## Tests and remaining limitations

- Final policy targeted tests: 166 local; 131 in its isolated network-disabled image.
- Additional diagnostic image tests: 93, including real `http.client.HTTPResponse` framing fixtures.
- Lead independently ran focused transport, recovery, schema, policy and diagnostic suites before committing.
- Broader image tests importing offline Batch cryptography failed because that inherited image lacks the dependency; these were not presented as an all-green suite. The offline Batch code was not changed in this rollout.
- No live 429 occurred in the successful request, so new 429-body diagnostics are fixture-tested but not yet exercised by a live error.

Reference: https://developers.openai.com/api/docs/guides/flex-processing documents uncharged resource-unavailable 429 responses and a 15-minute timeout example. This does not prove the unknown historical 429 was that error class.
