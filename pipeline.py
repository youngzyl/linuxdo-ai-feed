"""Cycle orchestration + scheduler: fetch -> details -> filter -> health bookkeeping."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from collector import Collector
from config import DATA_DIR, resolve_api_key
from filter import AuthError, Filter
from singleton import CycleLock
from store import now_iso


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Pipeline:
    def __init__(self, cfg: dict, store, logger):
        self.cfg = cfg
        self.store = store
        self.log = logger
        self.collector = Collector(cfg, store, logger)
        self.filter = Filter(cfg, store, logger)
        self.lock = threading.Lock()
        self.cycle_lock = CycleLock(DATA_DIR / ".cycle.lock")
        self.running = False

    # ---------------------------------------------------------------- one cycle
    def run_cycle(self, *, pages: int | None = None, do_detail: bool = True, do_filter: bool = True) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "skipped": "cycle already running"}
        if not self.cycle_lock.acquire():
            self.lock.release()
            self.log("cycle skipped: another process holds the cycle lock")
            return {"ok": False, "skipped": "another process holds the cycle lock"}
        try:
            self.running = True
            self.store.bump_cycles()
            self.store.health_update(last_attempt_at=now_iso(), status="running")
            result: dict = {"ok": False, "started_at": now_iso()}
            try:
                if not self.collector.categories:
                    self.collector.refresh_categories()
                listing = self.collector.fetch_list(pages=pages)
                upsert = self.store.upsert_topics(listing["topics"])
                fetch_summary = {
                    "ok": True,
                    "pages": listing["pages"],
                    "topics_seen": len(listing["topics"]),
                    "new": len(upsert["new_ids"]),
                    "error": None,
                    "page_errors": listing["page_errors"],
                    "list_source": listing.get("list_source"),
                    "finished_at": now_iso(),
                }
                # publish the list immediately so the page shows fresh titles while
                # the (slower) body fetch and the filter run
                self.store.section_replace("fetch", **fetch_summary)
                self.store.save()
                if do_detail:
                    # one RSS request covers the newest 30 bodies. Pace it: firing it right
                    # after the list pages trips Cloudflare's limiter and sends us to the
                    # slow proxy path for every body.
                    time.sleep(float(self.cfg["fetch"].get("rss_delay_s", 3.0)))
                    try:
                        rss = self.collector.fetch_rss_bodies()
                        fetch_summary.update(self.collector.apply_rss_bodies(rss))
                    except Exception as exc:
                        fetch_summary["rss_error"] = str(exc)
                        self.store.add_failure("fetch_rss", str(exc))
                        self.log(f"rss fetch failed (non-fatal): {exc}")
                    self.store.section_replace("fetch", **fetch_summary)
                    self.store.save()
                result["fetch"] = fetch_summary
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.store.add_failure("fetch_cycle", message)
                self.store.section_replace("fetch", ok=False, error=message, finished_at=now_iso())
                count = self.store.record_failure(message)
                backoff_min = min(self.cfg["interval_minutes"] * (2 ** min(count, 4)), 30)
                self.store.health_update(next_retry_at=(_utcnow() + timedelta(minutes=backoff_min)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                self.store.save()
                result.update(ok=False, error=message, consecutive_failures=count, next_retry_minutes=backoff_min)
                self.log(f"cycle FAILED (consecutive={count}, next in {backoff_min}m): {message}")
                return result

            result["ok"] = True
            self.store.record_success()
            self.store.health_update(next_retry_at=None)

            # Filter as soon as the list (+ whatever bodies arrived) is in, so the page
            # has verdicts within seconds; body fetches for the rest follow afterwards.
            if do_filter:
                key, key_source = resolve_api_key()
                summary = self.filter.run()
                self.store.section_replace("filter", **summary)
                result["filter"] = summary
                if not summary["key_present"]:
                    self.store.health_update(status="degraded")
                elif summary["ok"]:
                    self.store.health_update(status="ok", filter_consecutive_errors=0)
                else:
                    with self.store.lock:
                        errors = int(self.store.data["health"].get("filter_consecutive_errors") or 0) + 1
                    self.store.health_update(status="degraded", filter_consecutive_errors=errors)
                self.store.save()
            else:
                self.store.section_update("filter", ok=None)

            if do_detail:
                try:
                    need = [
                        t["id"]
                        for t in self.store.topics()
                        if not t.get("detail_fetched") and t.get("state") in ("pending", "picked")
                    ][: int(self.cfg["fetch"].get("detail_max_per_cycle", 40))]
                    if need:
                        details = self.collector.ensure_details(need)
                        fetch_summary.update(details)
                        self.store.section_replace("fetch", **fetch_summary)
                        self.store.save()
                except Exception as exc:
                    self.store.add_failure("detail_cycle", f"{type(exc).__name__}: {exc}")
                    self.log(f"detail phase failed (non-fatal): {exc}")

            # clear the "new" flag used by the UI animation bookkeeping
            fetch_summary["finished_at"] = fetch_summary.get("finished_at") or now_iso()
            self.store.section_replace("fetch", **fetch_summary)
            self.store.save()
            self.log(
                "cycle ok: fetched {seen} ({new} new), judged {judged}, picked {picked}".format(
                    seen=result["fetch"]["topics_seen"],
                    new=result["fetch"]["new"],
                    judged=(result.get("filter") or {}).get("judged", 0),
                    picked=(result.get("filter") or {}).get("picked", 0),
                )
            )
            return result
        finally:
            self.running = False
            self.cycle_lock.release()
            self.lock.release()

    # ------------------------------------------------------------------ scheduler
    def next_delay_seconds(self) -> float:
        health = self.store.health()
        failures = int(health.get("consecutive_failures") or 0)
        base = float(self.cfg["interval_minutes"]) * 60
        if failures:
            return min(base * (2 ** min(failures, 4)), 30 * 60)
        return base


class Scheduler(threading.Thread):
    """Background loop: run a cycle every interval, with exponential backoff on failure."""

    def __init__(self, pipeline: Pipeline, logger):
        super().__init__(name="scheduler", daemon=True)
        self.pipeline = pipeline
        self.log = logger
        self._stop = threading.Event()

    def run(self) -> None:
        # first cycle shortly after boot so a fresh start shows real data fast
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            try:
                result = self.pipeline.run_cycle()
                if not result.get("ok"):
                    self.log(f"scheduler: cycle failed: {result.get('error') or result.get('skipped')}")
            except Exception as exc:  # never let the loop die
                self.log(f"scheduler: unexpected error: {type(exc).__name__}: {exc}")
                self.pipeline.store.add_failure("scheduler", f"{type(exc).__name__}: {exc}")
            delay = self.pipeline.next_delay_seconds()
            self.log(f"scheduler: next cycle in {delay / 60:.1f} min")
            if self._stop.wait(delay):
                return

    def stop(self) -> None:
        self._stop.set()
