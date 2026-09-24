"""State store: topics, filter verdicts, queue, health counters.

Single JSON file (data/state.json) written atomically; failure journal is JSONL
(logs/failures.jsonl) because the RCA watchdog reads it tail-first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from config import owner_token, redact

VERSION = 1
FAILURE_LOG_CAP = 2000  # lines kept on disk

# Explicit owner feedback. `manual_override` is null / keep / skip - there is deliberately
# no read/bookmark value here: 收藏 is the queue's own membership (U01) and read state is
# browser-local, so neither is server state.
OVERRIDE_KEEP = "keep"
OVERRIDE_SKIP = "skip"
OVERRIDES = frozenset({OVERRIDE_KEEP, OVERRIDE_SKIP})

# Source-version marker: "<scheme>:<hex>". The length and the digest are computed here,
# never restated by callers or tests.
SOURCE_VERSION_SCHEME = "sv1"
SOURCE_VERSION_HEX = 16


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_iso(value: str | None):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def normalize_content(value) -> str:
    """Whitespace/width-normalized text for the source fingerprint.

    Width and whitespace are normalized so re-flowing the same text is not a new version.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def source_fingerprint(topic: dict) -> str:
    """Content version of what the filter actually judges.

    Title plus the body once we have one, otherwise the listing excerpt. Popularity and
    housekeeping fields (views / reply_count / like_count / bumped_at / detail_fetched)
    are excluded on purpose: they change on every refresh without changing the text, and
    treating them as new content would re-judge the whole feed on every cycle.
    """
    title = normalize_content(topic.get("title"))
    body = normalize_content(topic.get("body_text")) or normalize_content(topic.get("excerpt"))
    payload = f"title:{title}\nbody:{body}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SOURCE_VERSION_HEX]
    return f"{SOURCE_VERSION_SCHEME}:{digest}"


def effective_state(model_valuable, manual_override) -> str:
    """Reference rule: an explicit override outranks the model classification.

    `model_valuable` is the model's own verdict (True/False/None when it never judged).
    """
    if manual_override == OVERRIDE_KEEP:
        return "picked"
    if manual_override == OVERRIDE_SKIP:
        return "rejected"
    if model_valuable is None:
        return "pending"
    return "picked" if model_valuable else "rejected"


def public_error(value, limit: int = 70) -> str | None:
    """Sanitized, truncated error text safe for an unauthenticated response.

    `/health` is public (the watchdog and the page read it), so raw upstream text must not
    be echoed: quotes/newlines/control characters go away, credentials are redacted and the
    result is capped. The full text stays in data/state.json and logs/failures.jsonl, which
    are only reachable through the authenticated surface.
    """
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    text = redact(text)
    text = re.sub(r"[^A-Za-z0-9_. :/\-\[\]]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or None


def live_key_present(max_age_s: float = 5.0) -> bool:
    """Is a filter API key resolvable *right now*?

    Stored health only reflects the last filter run, so a freshly exported key would
    otherwise look missing until the next cycle finishes (minutes later) and the
    watchdog would raise a false 'no key' alarm.
    """
    global _KEY_CACHE
    now = time.time()
    if _KEY_CACHE and now - _KEY_CACHE[0] < max_age_s:
        return _KEY_CACHE[1]
    try:
        from config import resolve_api_key

        key, _ = resolve_api_key()
        present = bool(key)
    except Exception:
        present = False
    _KEY_CACHE = (now, present)
    return present


_KEY_CACHE: tuple[float, bool] | None = None


def default_health() -> dict:
    return {
        "status": "starting",
        "consecutive_failures": 0,
        "filter_consecutive_errors": 0,
        "last_error": None,
        "last_success_at": None,
        "last_attempt_at": None,
        "next_retry_at": None,
        "cycles_total": 0,
        "cycles_failed": 0,
        # stage success timestamps (additive): the fetch stage and the filter stage keep
        # independent streaks, so each needs its own "last time this stage was fine".
        "fetch_last_success_at": None,
        "filter_last_success_at": None,
        "fetch": {
            "ok": None,
            "pages": 0,
            "topics_seen": 0,
            "new": 0,
            "error": None,
            "detail_ok": 0,
            "detail_failed": 0,
            "detail_via_jina": 0,
            "finished_at": None,
        },
        "filter": {
            "ok": None,
            "model": None,
            "key_present": False,
            "key_source": None,
            "last_error": None,
            "batches_ok": 0,
            "batches_failed": 0,
            "judged": 0,
            "finished_at": None,
        },
    }


class Store:
    def __init__(self, path: Path | str, failure_log: Path | str, memory_failures: int = 300):
        self.path = Path(path)
        self.failure_log = Path(failure_log)
        self.feedback_log = self.path.parent / "feedback.jsonl"
        self.memory_failures = memory_failures
        self.lock = threading.RLock()
        self.data: dict = {"version": VERSION, "topics": {}, "queue": [], "feedback": [], "health": default_health()}
        self.failures: list[dict] = []
        self.started_at = time.time()
        self.load()

    # ---------------------------------------------------------------- persistence
    def load(self) -> None:
        with self.lock:
            if self.path.exists():
                try:
                    blob = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    blob = {}
                if isinstance(blob, dict):
                    self.data["topics"] = blob.get("topics") or {}
                    self.data["queue"] = [int(x) for x in (blob.get("queue") or [])]
                    self.data["feedback"] = list(blob.get("feedback") or [])
                    health = default_health()
                    health.update(blob.get("health") or {})
                    for section in ("fetch", "filter"):
                        merged = default_health()[section]
                        merged.update((blob.get("health") or {}).get(section) or {})
                        health[section] = merged
                    self.data["health"] = health
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load_failures()
            self._merge_feedback_journal()
            self._migrate_state()
        if not self.path.exists():
            self.save()

    def _merge_feedback_journal(self) -> None:
        """Votes are the one thing here that cannot be re-fetched, so they are appended to
        data/feedback.jsonl as well. Anything in the journal that state.json lost (a stale
        process can overwrite state.json with an older in-memory snapshot) comes back."""
        if not self.feedback_log.exists():
            return
        best: dict[int, dict] = {}
        for row in self.data.get("feedback") or []:
            try:
                best[int(row["id"])] = row
            except (KeyError, TypeError, ValueError):
                continue
        try:
            lines = self.feedback_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return
        for line in lines:
            try:
                row = json.loads(line)
                tid = int(row["id"])
            except Exception:
                continue
            if row.get("vote") == "clear":
                best.pop(tid, None)
                continue
            current = best.get(tid)
            if not current or (row.get("at") or "") >= (current.get("at") or ""):
                best[tid] = row
        self.data["feedback"] = sorted(best.values(), key=lambda r: r.get("at") or "")

    def _migrate_state(self) -> None:
        """Bring legacy records up to the model-verdict / manual-override split.

        Two things must not happen here:

        * An override is derived **only** from an actual feedback record. The
          `rescued`/`skipped` topic flags are display hints (three topics in the live
          state file carry them with no vote row at all), so they never become intent.
        * The legacy backlog must not turn into new work. An existing verdict is bound to
          the topic's current source version, i.e. treated as "the version it judged";
          only later content changes then invalidate it.
        """
        feedback: dict[int, dict] = {}
        votes: dict[int, str] = {}
        for row in self.data.get("feedback") or []:
            if not isinstance(row, dict):
                continue
            try:
                tid = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            feedback[tid] = row
            vote = str(row.get("vote") or "").strip().lower()
            if vote in OVERRIDES:
                votes[tid] = vote

        for key, topic in self.data["topics"].items():
            if not isinstance(topic, dict):
                continue
            try:
                tid = int(key)
            except (TypeError, ValueError):
                tid = None
            if "manual_override" not in topic and tid is not None and tid in votes:
                topic["manual_override"] = votes[tid]
            self._touch_source_version(topic)
            filt = topic.get("filter")
            if isinstance(filt, dict):
                if not isinstance(filt.get("valuable"), bool):
                    filt["valuable"] = self._legacy_model_value(topic, feedback.get(tid if tid is not None else -1))
                if not filt.get("source_version"):
                    filt["source_version"] = topic["source_version"]
            self._refresh_state(topic)

    def _legacy_model_value(self, topic: dict, row: dict | None):
        """What the model itself decided, for records written before `valuable` existed.

        The vote row remembers the state at vote time, which is the model's own decision
        even when the owner then overrode it. For an untouched topic the stored state was
        the model's decision by construction. Anything else stays unknown (None), which
        simply makes the topic eligible for a fresh verdict.
        """
        at_vote = (row or {}).get("state_at_vote")
        if at_vote in ("picked", "rejected"):
            return at_vote == "picked"
        if self.manual_override(topic):
            return None  # overridden with no surviving vote record: do not guess a value
        state = topic.get("state")
        if state in ("picked", "rejected"):
            return state == "picked"
        return None

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def _load_failures(self) -> None:
        self.failures = []
        if not self.failure_log.exists():
            return
        try:
            lines = self.failure_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return
        for line in lines[-self.memory_failures :]:
            try:
                self.failures.append(json.loads(line))
            except Exception:
                continue

    # ------------------------------------------------------------------- failures
    def add_failure(self, stage: str, error: str, *, attempt: int | None = None, context: dict | None = None) -> dict:
        entry = {
            "at": now_iso(),
            "stage": stage,
            "attempt": attempt,
            "error": redact(str(error))[:600],
            "context": context or {},
        }
        with self.lock:
            self.failures.append(entry)
            self.failures = self.failures[-self.memory_failures :]
            try:
                self.failure_log.parent.mkdir(parents=True, exist_ok=True)
                with self.failure_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if self.failure_log.stat().st_size > 400_000:  # keep the journal small
                    lines = self.failure_log.read_text(encoding="utf-8", errors="ignore").splitlines()
                    self.failure_log.write_text(
                        "\n".join(lines[-FAILURE_LOG_CAP:]) + "\n", encoding="utf-8"
                    )
            except Exception:
                pass
        return entry

    def recent_failures(self, n: int = 20) -> list[dict]:
        with self.lock:
            return list(reversed(self.failures[-max(1, n) :]))

    # --------------------------------------------------------------------- topics
    def upsert_topics(self, topics: list[dict]) -> dict:
        """Insert new topics, refresh volatile counters, keep existing filter verdicts."""
        new_ids: list[int] = []
        with self.lock:
            for topic in topics:
                tid = int(topic["id"])
                existing = self.data["topics"].get(str(tid))
                if not existing:
                    record = dict(topic)
                    record.setdefault("state", "pending")
                    record.setdefault("filter", None)
                    record.setdefault("body_text", "")
                    record.setdefault("detail_fetched", False)
                    record.setdefault("first_seen_at", now_iso())
                    self._refresh_state(record)
                    self._touch_source_version(record)
                    self.data["topics"][str(tid)] = record
                    new_ids.append(tid)
                    continue
                for key in ("title", "reply_count", "views", "like_count", "bumped_at", "tags", "category", "excerpt"):
                    if topic.get(key) not in (None, "", []):
                        existing[key] = topic[key]
                if topic.get("body_text") and not existing.get("body_text"):
                    existing["body_text"] = topic["body_text"]
                # title/excerpt/body may have changed: keep the content version honest.
                # Popularity-only refreshes land on the same fingerprint.
                self._touch_source_version(existing)
        return {"new_ids": new_ids, "total": len(self.data["topics"])}

    def get(self, tid: int | str) -> dict | None:
        with self.lock:
            return self.data["topics"].get(str(tid))

    def topics(self) -> list[dict]:
        with self.lock:
            return list(self.data["topics"].values())

    def pending(self, limit: int | None = None) -> list[dict]:
        """Topics that still need a model verdict for their current source version.

        This is eligibility for *work*, not the display state: a topic whose body was
        enriched after it was judged comes back here even when the owner's override still
        rules the display (see `needs_model_verdict`).
        """
        with self.lock:
            items = [t for t in self.data["topics"].values() if self.needs_model_verdict(t)]
        items.sort(key=lambda t: (t.get("created_at") or "", int(t["id"])), reverse=True)
        return items[:limit] if limit else items

    # ------------------------------------------------- model verdict vs. override
    @staticmethod
    def model_value(topic: dict):
        """The model's own verdict (True/False), independent of any manual override."""
        value = (topic.get("filter") or {}).get("valuable")
        return value if isinstance(value, bool) else None

    @staticmethod
    def manual_override(topic: dict):
        """The owner's explicit override for this topic: keep / skip / None."""
        override = topic.get("manual_override")
        return override if override in OVERRIDES else None

    @staticmethod
    def verdict_version(topic: dict):
        """Source version the stored model verdict was produced from (None: legacy)."""
        return (topic.get("filter") or {}).get("source_version")

    def source_version(self, topic: dict) -> str:
        return source_fingerprint(topic)

    def needs_model_verdict(self, topic: dict) -> bool:
        """No verdict yet, a verdict for older bytes, or a legacy record with no value."""
        if self.model_value(topic) is None:
            return True
        return self.verdict_version(topic) != self.source_version(topic)

    def _refresh_state(self, topic: dict) -> str:
        """Recompute the derived display state from (model value, override)."""
        topic["state"] = effective_state(self.model_value(topic), self.manual_override(topic))
        return topic["state"]

    def _touch_source_version(self, topic: dict) -> str:
        version = source_fingerprint(topic)
        topic["source_version"] = version
        return version

    def set_manual_override(self, tid: int | str, override) -> str:
        """Set/clear the explicit override. Returns the resulting display state."""
        if override is not None and override not in OVERRIDES:
            raise ValueError(f"manual_override must be null/keep/skip, got {override!r}")
        with self.lock:
            topic = self.data["topics"].get(str(tid))
            if not topic:
                return "unknown_topic"
            if override is None:
                topic.pop("manual_override", None)
            else:
                topic["manual_override"] = override
            return self._refresh_state(topic)

    def clear_manual_override(self, tid: int | str) -> str:
        """Drop the override: the latest model verdict (or pending) is exposed again."""
        return self.set_manual_override(tid, None)

    def set_verdict(self, tid: int | str, verdict: dict, *, source_version: str | None = None) -> str:
        """Apply a model verdict. Returns 'applied' | 'stale' | 'unknown_topic'.

        The override is untouched here: an explicit keep/skip keeps ruling the display
        while the model verdict - and its provenance - is still recorded. When
        `source_version` is supplied (the version captured when the batch was dispatched)
        it is compared against the topic's current version, so an answer for older bytes
        is refused and the topic stays eligible for the current version.
        """
        with self.lock:
            topic = self.data["topics"].get(str(tid))
            if not topic:
                return "unknown_topic"
            current = self._touch_source_version(topic)
            if source_version is not None and source_version != current:
                return "stale"
            raw_value = verdict.get("valuable")
            topic["filter"] = {
                "valuable": raw_value if isinstance(raw_value, bool) else None,
                "score": verdict.get("score"),
                "category": verdict.get("category"),
                "reason": verdict.get("reason"),
                "summary": verdict.get("summary"),
                "model": verdict.get("model"),
                "prompt_version": verdict.get("prompt_version"),
                "source_version": current,
                "at": now_iso(),
            }
            self._refresh_state(topic)
            return "applied"

    def set_fields(self, tid: int | str, **fields) -> None:
        with self.lock:
            topic = self.data["topics"].get(str(tid))
            if topic:
                topic.update(fields)
                # body enrichment (RSS upsert / detail fetch) must move the version with
                # the content, otherwise the next batch would judge stale bytes
                self._touch_source_version(topic)

    # ---------------------------------------------------------------------- queue
    def queue(self) -> list[int]:
        with self.lock:
            return list(self.data["queue"])

    def queue_add(self, tid: int) -> list[int]:
        with self.lock:
            if int(tid) not in self.data["queue"]:
                self.data["queue"].append(int(tid))
            return list(self.data["queue"])

    def queue_remove(self, tid: int) -> list[int]:
        with self.lock:
            self.data["queue"] = [x for x in self.data["queue"] if x != int(tid)]
            return list(self.data["queue"])

    def queue_set(self, ids: list[int]) -> list[int]:
        with self.lock:
            clean = []
            for x in ids:
                try:
                    xi = int(x)
                except Exception:
                    continue
                if xi not in clean and str(xi) in self.data["topics"]:
                    clean.append(xi)
            self.data["queue"] = clean
            return list(clean)

    # ------------------------------------------------------------------- feedback
    def add_feedback(self, entry: dict) -> list[dict]:
        """Replace this topic's previous vote and append it to the durable journal."""
        tid = int(entry["id"])
        with self.lock:
            rows = [r for r in (self.data.get("feedback") or []) if int(r.get("id") or 0) != tid]
            rows.append(entry)
            self.data["feedback"] = rows[-200:]
            try:
                self.feedback_log.parent.mkdir(parents=True, exist_ok=True)
                with self.feedback_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
            return list(self.data["feedback"])

    def clear_feedback(self, tid: int) -> list[dict]:
        tid = int(tid)
        with self.lock:
            self.data["feedback"] = [r for r in (self.data.get("feedback") or []) if int(r.get("id") or 0) != tid]
            try:
                with self.feedback_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"id": tid, "vote": "clear", "at": now_iso()}, ensure_ascii=False) + "\n")
            except Exception:
                pass
            return list(self.data["feedback"])

    def feedback_rows(self) -> list[dict]:
        with self.lock:
            return list(self.data.get("feedback") or [])

    # --------------------------------------------------------------------- health
    def health(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data["health"]))

    def health_update(self, **fields) -> dict:
        with self.lock:
            self.data["health"].update(fields)
            return self.health()

    def section(self, name: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data["health"][name]))

    def section_update(self, name: str, **fields) -> dict:
        with self.lock:
            self.data["health"][name].update(fields)
            return self.section(name)

    def section_replace(self, name: str, **fields) -> dict:
        """Reset a section to its defaults before applying this cycle's fields, so a
        stale value (e.g. last cycle's proxy error) cannot linger."""
        with self.lock:
            merged = default_health()[name]
            merged.update(fields)
            self.data["health"][name] = merged
            return self.section(name)

    def record_fetch_success(self) -> None:
        """A successful *fetch* stage. Clears the fetch streak and only that one.

        This runs before the filter stage (pipeline.py), so it must not touch
        `filter_consecutive_errors`: doing so reset the filter failure streak on every
        healthy cycle, and the filter could fail forever without `/health.attention`
        (or the watchdog reading it) ever noticing.
        """
        self.health_update(
            last_success_at=now_iso(),
            fetch_last_success_at=now_iso(),
            consecutive_failures=0,
            last_error=None,
            status="ok",
        )

    def record_success(self) -> None:
        """Backwards-compatible alias for the fetch-stage success (legacy callers/tests)."""
        self.record_fetch_success()

    def record_filter_success(self) -> None:
        """A successful filter run. Clears the filter streak and only that one."""
        self.health_update(
            filter_consecutive_errors=0,
            filter_last_success_at=now_iso(),
            status="ok",
        )

    def record_filter_failure(self, error: str | None = None) -> int:
        """A failed filter run: grow the filter streak, keep the fetch streak intact.

        Returns the new filter streak so a caller can log it.
        """
        with self.lock:
            health = self.data["health"]
            errors = int(health.get("filter_consecutive_errors") or 0) + 1
            health["filter_consecutive_errors"] = errors
            if error:
                health["filter"]["last_error"] = redact(str(error))[:600]
            health["status"] = "degraded"
            return errors

    def record_failure(self, error: str) -> int:
        with self.lock:
            health = self.data["health"]
            health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
            health["cycles_failed"] = int(health.get("cycles_failed") or 0) + 1
            health["last_error"] = redact(str(error))[:600]
            health["status"] = "failing"
            return health["consecutive_failures"]

    def bump_cycles(self) -> None:
        with self.lock:
            self.data["health"]["cycles_total"] = int(self.data["health"].get("cycles_total") or 0) + 1

    # ------------------------------------------------------------------ API views
    def counts(self) -> dict:
        with self.lock:
            topics = list(self.data["topics"].values())
        picked = sum(1 for t in topics if t.get("state") == "picked")
        rejected = sum(1 for t in topics if t.get("state") == "rejected")
        pending = sum(1 for t in topics if t.get("state") == "pending")
        return {"all": len(topics), "picked": picked, "rejected": rejected, "pending": pending, "queue": len(self.data["queue"])}

    @staticmethod
    def topic_brief(topic: dict, *, include_body: bool = True) -> dict:
        """One list-row topic for /api/state, always with an added `has_body` flag.

        `include_body=False` is the body-free list shape (?view=list, CONTRACT.md §1.1): the
        body is why the payload reached 2.7 MB and the list view shows none of it, so it is
        dropped and fetched per topic from /api/topic/<id>. `include_body=True` (the default)
        is the legacy shape an older tab still expects - without it, a client built before
        the split would render every topic as if it had no body.

        Either shape is a fresh copy, never the live dict: a caller must not be able to mutate
        the stored state, and dropping a key from the original would delete the body itself.
        """
        brief = {
            key: value
            for key, value in topic.items()
            if include_body or key != "body_text"
        }
        brief["has_body"] = bool(str(topic.get("body_text") or "").strip())
        return brief

    def api_payload(self, *, cfg: dict, uptime_s: float, include_body: bool = True) -> dict:
        with self.lock:
            topics = [
                self.topic_brief(t, include_body=include_body)
                for t in sorted(
                    self.data["topics"].values(),
                    key=lambda t: (t.get("created_at") or "", int(t["id"])),
                    reverse=True,
                )
            ]
            health = json.loads(json.dumps(self.data["health"]))
            fetched_at = health.get("fetch", {}).get("finished_at")
            filter_meta = health.get("filter", {})
        counts = self.counts()
        return {
            "generated_at": now_iso(),
            "source": {
                "tag": cfg["tag"],
                "tag_url": cfg["tag_page_url"],
                "upstream": cfg["tag_url"],
                "fetched_at": fetched_at,
                "total_topics": counts["all"],
            },
            "filter": {
                "model": cfg["filter"]["model"],
                "prompt_version": cfg["filter"]["prompt_version"],
                "last_run_at": filter_meta.get("finished_at"),
                "picked": counts["picked"],
                "rejected": counts["rejected"],
                "pending": counts["pending"],
                "errors": filter_meta.get("batches_failed") or 0,
                "key_present": bool(filter_meta.get("key_present")) or live_key_present(),
            },
            "counts": counts,
            "queue": list(self.data["queue"]),
            "feedback": {
                "keep": sum(1 for r in (self.data.get("feedback") or []) if r.get("vote") == "keep"),
                "skip": sum(1 for r in (self.data.get("feedback") or []) if r.get("vote") == "skip"),
                "total": len(self.data.get("feedback") or []),
            },
            "uptime_s": round(uptime_s, 1),
            "topics": topics,
        }

    def attention(self, cfg: dict) -> dict:
        health = self.health()
        threshold = int(cfg.get("attention_threshold", 3))
        filter_threshold = int(cfg.get("filter_consecutive_error_threshold", 3))
        pending = self.counts()["pending"]
        if health["consecutive_failures"] >= threshold:
            return {
                "needed": True,
                "reason": f"fetch failed {health['consecutive_failures']} cycles in a row: {public_error(health.get('last_error')) or '-'}",
                "kind": "fetch_failing",
            }
        if health["filter_consecutive_errors"] >= filter_threshold:
            return {
                "needed": True,
                "reason": f"filter errored {health['filter_consecutive_errors']} cycles in a row: {public_error(health.get('filter', {}).get('last_error')) or '-'}",
                "kind": "filter_failing",
            }
        if pending > 0 and not (health["filter"].get("key_present") or live_key_present()):
            return {
                "needed": True,
                "reason": "no CommandCode API key (env commandcode_apikey) - filter cannot run",
                "kind": "no_key",
            }
        return {"needed": False, "reason": "", "kind": None}

    def health_payload(self, *, cfg: dict, uptime_s: float) -> dict:
        health = self.health()
        att = self.attention(cfg)
        consecutive = health["consecutive_failures"]
        status = "ok"
        if consecutive:
            status = "failing" if att["kind"] == "fetch_failing" else "degraded"
        elif health.get("fetch", {}).get("ok") is False or health.get("filter", {}).get("ok") is False:
            status = "degraded"
        token, _ = owner_token()
        fetch = dict(health.get("fetch") or {})
        filt = dict(health.get("filter") or {})
        # public payload: raw upstream text is replaced by a sanitized signature, and the
        # structured summary carries what a monitor actually needs
        fetch["error"] = public_error(fetch.get("error"))
        filt["last_error"] = public_error(filt.get("last_error"))
        filt["error"] = public_error(filt.get("error"))
        return {
            "status": status,
            "attention": att,
            "uptime_s": round(uptime_s, 1),
            "last_success_at": health.get("last_success_at"),
            "last_attempt_at": health.get("last_attempt_at"),
            "consecutive_failures": consecutive,
            "next_retry_at": health.get("next_retry_at"),
            "cycles_total": health.get("cycles_total"),
            "cycles_failed": health.get("cycles_failed"),
            "counts": self.counts(),
            "fetch": fetch,
            "filter": filt,
            "last_error": public_error(health.get("last_error")),
            "summary": {
                "fetch_ok": fetch.get("ok"),
                "topics_seen": fetch.get("topics_seen"),
                "filter_ok": filt.get("ok"),
                "judged": filt.get("judged"),
                "picked": filt.get("picked"),
                "batches_failed": filt.get("batches_failed"),
                "filter_consecutive_errors": health.get("filter_consecutive_errors"),
                "attention": att.get("kind"),
            },
            "auth": {
                # never the token itself: only whether writes are possible at all
                "writes": "owner" if token else "disabled",
                "token_configured": bool(token),
            },
        }

    def metrics_text(self, *, cfg: dict) -> str:
        health = self.health()
        counts = self.counts()
        att = self.attention(cfg)
        last_success = parse_iso(health.get("last_success_at"))
        ts = last_success.timestamp() if last_success else 0
        lines = [
            "# HELP linuxdo_ai_fetch_consecutive_failures Consecutive failed fetch cycles.",
            "# TYPE linuxdo_ai_fetch_consecutive_failures gauge",
            f"linuxdo_ai_fetch_consecutive_failures {health['consecutive_failures']}",
            "# TYPE linuxdo_ai_topics_total gauge",
            f"linuxdo_ai_topics_total {counts['all']}",
            "# TYPE linuxdo_ai_picked_total gauge",
            f"linuxdo_ai_picked_total {counts['picked']}",
            "# TYPE linuxdo_ai_pending_total gauge",
            f"linuxdo_ai_pending_total {counts['pending']}",
            "# TYPE linuxdo_ai_attention_needed gauge",
            f"linuxdo_ai_attention_needed {1 if att['needed'] else 0}",
            "# TYPE linuxdo_ai_last_success_timestamp_seconds gauge",
            f"linuxdo_ai_last_success_timestamp_seconds {ts:.0f}",
            "# TYPE linuxdo_ai_cycles_total counter",
            f'linuxdo_ai_cycles_total{{result="ok"}} {int(health.get("cycles_total") or 0) - int(health.get("cycles_failed") or 0)}',
            f'linuxdo_ai_cycles_total{{result="fail"}} {int(health.get("cycles_failed") or 0)}',
        ]
        return "\n".join(lines) + "\n"
