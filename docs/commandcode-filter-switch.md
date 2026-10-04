# Production filter corrected to CommandCode — 2026-10-04

## Scope and cause

The user originally specified CommandCode `deepseek/deepseek-v4.1-flash`. A temporary DeepSeek-direct `deepseek-chat` configuration persisted into the bwgca migration and failed with HTTP 402. The project key existed in research.env but the feed did not load it. This was a production-configuration omission, not a model fallback. The user authorized the switch.

## Applied configuration

- Target: bwgca Compose project `linuxdo-ai`, service `feed`.
- Base URL: `https://api.commandcode.ai/provider/v1`.
- Model: `deepseek/deepseek-v4.1-flash` (not the separate fast tier).
- Key resolution: `env:commandcode_apikey`.
- Only the project's existing CommandCode key was installed in mode-0600 service.env; unrelated research variables and legacy variables were preserved. compose.env changed only filter base/model.
- Applied at `2026-10-04T04:05:08Z`. Recreated only feed, with no dependency/image change. Image remains `linuxdo-ai-feed:tcstw-migration-caf2efe`.
- Remote backups: `/home/youngzyl/services/linuxdo-ai/cc-switch-backup-20261004T040508Z`, directory 0700, files 0600. Rollback script staged separately; not executed.

## Real execution evidence

A bounded synthetic preflight from the credential-owning host used the app's exact User-Agent, verified TLS and refused redirects. HTTP 200, returned exact requested model, and all JSON-schema checks passed. The probe wrote no production article data.

The resident scheduler's first post-restart real filter cycle finished at `2026-10-04T04:05:52Z`: 88 judgments, 13 picked, 11 successful batches, 0 failed batches. Pending decreased from 87 to 0. Bookmarks stayed 30, feedback log entries stayed 5; no historical reset or forced rejudge.

Lead independently read back the public bwgca /health over verified TLS with the Pages Origin: HTTP 200, matching CORS, status `ok`, attention false, filter.ok true, exact new model/key source, judged 88 and no failed batches. A separate container config readback confirmed the CommandCode base and healthy state. This proves the first recovered cycle, not indefinite future uptime.

Local secret-free evidence: `/workspace/cc-switch-20261004/{switch-result.json,public-verification.json,apply.out}`. Credentials and env file contents are not included in Git, Pages, chat, or build artifacts. No Hermes model/auth change, research scheduling, UI or telemetry change was included.
