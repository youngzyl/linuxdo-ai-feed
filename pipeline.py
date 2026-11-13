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
        if type(cfg.get('filter', {}).get('enabled', True)) is not bool:
            raise ValueError('filter.enabled must be boolean')
        self.store = store
        self.log = logger
        self.collector = Collector(cfg, store, logger)
        flex_cfg = cfg.get('future_flex') or {}
        if type(flex_cfg.get('enabled', False)) is not bool:
            raise ValueError('future_flex.enabled must be boolean')
        if flex_cfg.get('enabled'):
            if flex_cfg.get('images_enabled') is not True:
                raise ValueError('future_flex requires images_enabled=true')
            if not cfg.get('filter', {}).get('enabled', True):
                raise ValueError('future_flex requires filter.enabled')
            from future_flex import FutureFlex
            self.filter = FutureFlex(cfg, store, logger)
        else:
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
            # fetch stage only: the filter has not run yet, so this must not clear the
            # filter failure streak (see Store.record_fetch_success)
            self.store.record_fetch_success()
            self.store.health_update(next_retry_at=None)

            # Filter as soon as the list (+ whatever bodies arrived) is in, so the page
            # has verdicts within seconds; body fetches for the rest follow afterwards.
            if do_filter and self.cfg.get('filter', {}).get('enabled', True):
                summary = self.filter.run()
                self.store.section_replace("filter", **summary)
                result["filter"] = summary
                if summary.get("partial"):
                    # an accepted batch with holes: visible in health AND in the RCA journal,
                    # so a slowly degrading model answer is not silently a "success"
                    self.store.add_failure(
                        "filter_partial",
                        str(summary.get("error") or "partial filter result"),
                        context={
                            name: summary.get(name)
                            for name in ("missing", "invalid", "ambiguous", "unknown", "judged", "batches_partial")
                        },
                    )
                    self.log(
                        "filter partial: {judged} judged, {missing} missing, {invalid} invalid, "
                        "{ambiguous} ambiguous, {unknown} unknown".format(
                            judged=summary.get("judged", 0),
                            missing=summary.get("missing", 0),
                            invalid=summary.get("invalid", 0),
                            ambiguous=summary.get("ambiguous", 0),
                            unknown=summary.get("unknown", 0),
                        )
                    )
                if not summary["key_present"]:
                    self.store.health_update(status="degraded")
                elif summary["ok"]:
                    self.store.record_filter_success()
                else:
                    # the filter stage keeps its own streak: consecutive fetch-ok /
                    # filter-fail cycles must reach the attention threshold. A partial
                    # result lands here too - it must not reset the streak.
                    self.store.record_filter_failure(summary.get("error"))
                self.store.save()
            elif do_filter:
                disabled = {'ok': None, 'enabled': False, 'skipped': 'disabled', 'judged': 0, 'batches_failed': 0}
                self.store.section_replace('filter', **disabled)
                result['filter'] = disabled
            else:
                self.store.section_update("filter", ok=None)

            if do_detail:
                try:
                    flex_cfg = self.cfg.get('future_flex') or {}
                    budget = max(0, int(self.cfg["fetch"].get("detail_max_per_cycle", 40)))
                    reinspection = []
                    if flex_cfg.get('enabled') is True and flex_cfg.get('images_enabled') is True:
                        from future_flex import secs, original_ids, backfill_ready, backfill_exclusions, attempted_current, image_signature
                        with self.store.lock:
                            meta = self.store.data.get('future_flex') or {}
                            excluded = original_ids(self.store.data) | set(meta.get('excluded_ids') or [])
                            t0 = flex_cfg.get('activation_t0')
                            bt = meta.get('backfill_t0')
                            history = (flex_cfg.get('backfill_enabled') is True and type(bt) is int and bt == flex_cfg.get('backfill_t0') and meta.get('backfill_prior_run_id') == flex_cfg.get('backfill_prior_run_id') and backfill_ready(self.store.data, meta.get('backfill_prior_run_id')))
                            history_excluded = backfill_exclusions(self.store.data, meta) if history else excluded
                            for topic in self.store.data['topics'].values():
                                tid = topic.get('id'); created = secs(topic.get('created_at')); first = secs(topic.get('first_seen_at'))
                                if type(tid) is not int or self.store._luna_reserved(tid) or isinstance((topic.get('filter') or {}).get('valuable'), bool) or attempted_current(topic):
                                    continue
                                retry = topic.get('future_flex_detail_retry') or {}
                                if time.time() < retry.get('next_retry', 0):
                                    continue
                                images = topic.get('images'); omitted = topic.get('images_omitted')
                                missing = topic.get('images_inspected') is not True or not isinstance(images, list) or type(omitted) is not int or omitted < 0 or any(not isinstance(x, str) for x in images)
                                future = type(t0) is int and created is not None and first is not None and created >= t0 and first >= t0 and tid not in excluded and (topic.get('image_inspection_pending') is True or (topic.get('detail_fetched') and missing))
                                historical = history and created is not None and bt-604800 <= created < bt and tid not in history_excluded and missing
                                if future or historical:
                                    reinspection.append(topic)
                            reinspection.sort(key=lambda t: (t.get('future_flex_reinspection_seq', 0), t['id']))
                            reinspect_ids = {x['id'] for x in reinspection}
                            def fresh_eligible(t):
                                retry = t.get('future_flex_detail_retry') or {}
                                blocked = time.time() < retry.get('next_retry', 0)
                                return not blocked and not t.get('detail_fetched') and t.get('state') in ('pending', 'picked') and t['id'] not in reinspect_ids
                            fresh = [t['id'] for t in self.store.topics() if fresh_eligible(t)]
                            quota = min(len(reinspection), max(1, budget // 4)) if budget else 0
                            # A single slot alternates when both queues have work.
                            if budget == 1 and fresh and reinspection and meta.get('future_flex_detail_turn') != 'reinspect':
                                quota = 0
                            chosen = reinspection[:quota]
                            need = []
                            fresh_selected = fresh[:max(0, budget-quota)]
                            fresh_first = bool(chosen and fresh_selected and meta.get('future_flex_detail_lead') == 'fresh')
                            for i in range(max(len(chosen), len(fresh_selected))):
                                if fresh_first and i < len(fresh_selected): need.append(fresh_selected[i])
                                if i < len(chosen): need.append(chosen[i]['id'])
                                if not fresh_first and i < len(fresh_selected): need.append(fresh_selected[i])
                            if chosen and fresh_selected:
                                meta['future_flex_detail_lead'] = 'reinspect' if fresh_first else 'fresh'
                            if budget == 1 and fresh and reinspection:
                                meta['future_flex_detail_turn'] = 'fresh' if quota else 'reinspect'
                            if chosen:
                                for topic in chosen:
                                    topic['image_inspection_pending'] = True
                            if chosen or (budget == 1 and fresh and reinspection):
                                self.store.save()
                    else:
                        need = [t['id'] for t in self.store.topics() if not t.get('detail_fetched') and t.get('state') in ('pending', 'picked')][:budget]
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
