"""Fetching layer for linux.do tag 人工智能 (read-only).

Two sources of truth:
  * list  : /tag/444-tag/444.json?order=created&ascending=false&page=N   (metadata)
  * detail: /t/topic/<id>.json                                           (OP body)
Detail requests are paced; when linux.do answers with a Cloudflare interstitial /
429 the request is retried through the r.jina.ai text proxy (still read-only).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from typing import Any

import http_util
from config import resolve_cookie

TAG_PLACEHOLDER = re.compile(r"<[^>]+>")
SCRIPT_BLOCK = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
BLOCK_BREAK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|blockquote|pre|td)\b[^>]*>", re.I)
EMOJI_SHORTCODE = re.compile(r":[a-z0-9_+-]+:", re.I)

# When the global rate-limit cooldown is longer than this, stop the current phase
# instead of sleeping through it: the rest is picked up on the next cycle.
_COOLDOWN_ABORT_S = 75.0


def html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = SCRIPT_BLOCK.sub(" ", raw_html)
    text = BLOCK_BREAK.sub("\n", text)
    text = TAG_PLACEHOLDER.sub("", text)
    text = html_mod.unescape(text)
    text = EMOJI_SHORTCODE.sub(" ", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_json_blob(text: str) -> Any:
    """Pull a JSON object out of a raw body or an r.jina.ai wrapper."""
    import json

    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"no JSON object found in response (head: {text[:120]!r})")


def linuxdo_headers() -> dict:
    headers = dict(http_util.BROWSER_HEADERS)
    cookie = resolve_cookie()
    if cookie:
        headers["cookie"] = cookie
    return headers


class Collector:
    def __init__(self, cfg: dict, store, logger):
        self.cfg = cfg
        self.store = store
        self.log = logger
        self.categories: dict[int, str] = {}
        self._load_categories_cache()
        self.last_detail_source = {"direct": 0, "jina": 0, "failed": 0}

    # ------------------------------------------------------------------ utilities
    def _load_categories_cache(self):
        path = self.cfg.get("_data_dir")
        try:
            from pathlib import Path

            cache = Path(path or ".") / "categories.json" if path else None
            if cache and cache.exists():
                import json

                self.categories = {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
        except Exception:
            self.categories = {}

    def refresh_categories(self) -> dict:
        import json
        from pathlib import Path

        try:
            blob = http_util.get_json(
                f"{self.cfg['site_url']}/categories.json",
                headers=linuxdo_headers(),
                timeout=self.cfg["request_timeout_s"],
            )
            cats = blob["category_list"]["categories"]
            self.categories = {int(c["id"]): (c.get("name") or c.get("slug")) for c in cats}
            data_dir = self.cfg.get("_data_dir")
            if data_dir:
                Path(data_dir).mkdir(parents=True, exist_ok=True)
                (Path(data_dir) / "categories.json").write_text(
                    json.dumps({str(k): v for k, v in self.categories.items()}, ensure_ascii=False),
                    encoding="utf-8",
                )
            self.log(f"categories refreshed: {len(self.categories)}")
        except Exception as exc:  # not fatal - category names degrade to ids
            self.store.add_failure("categories", str(exc))
            self.log(f"categories refresh failed (non-fatal): {exc}")
        return self.categories

    def _category_name(self, category_id) -> str:
        try:
            return self.categories.get(int(category_id)) or (f"#{category_id}" if category_id else "")
        except Exception:
            return ""

    # ---------------------------------------------------------------------- list
    def list_url(self, page: int) -> str:
        base = self.cfg["tag_url"]
        return f"{base}?order=created&ascending=false&page={page}"

    def fetch_page(self, page: int) -> dict:
        blob = http_util.get_json(
            self.list_url(page), headers=linuxdo_headers(), timeout=self.cfg["request_timeout_s"]
        )
        topic_list = blob.get("topic_list") or {}
        users = {u["id"]: u.get("username") or u.get("name") or "" for u in (topic_list.get("users") or [])}
        topics = []
        for raw in topic_list.get("topics") or []:
            topics.append(self._normalize(raw, users))
        more = topic_list.get("more_topics_url")
        return {"topics": topics, "more": more, "raw_count": len(topic_list.get("topics") or [])}

    def _normalize(self, raw: dict, users: dict) -> dict:
        posters = raw.get("posters") or []
        author = ""
        if posters:
            author = users.get(posters[0].get("user_id"), "") or ""
        if not author:
            author = raw.get("last_poster_username") or ""
        tags = [t.get("name") for t in (raw.get("tags") or []) if t.get("name")]
        tid = int(raw["id"])
        return {
            "id": tid,
            "title": html_mod.unescape(raw.get("title") or "").strip(),
            "url": f"{self.cfg['site_url']}/t/topic/{tid}",
            "author": author,
            "created_at": raw.get("created_at"),
            "bumped_at": raw.get("bumped_at"),
            "reply_count": raw.get("reply_count") or 0,
            "views": raw.get("views") or 0,
            "like_count": raw.get("like_count") or 0,
            "category": self._category_name(raw.get("category_id")),
            "tags": tags,
            "excerpt": html_to_text(raw.get("excerpt") or "")[:300],
            "bumped": raw.get("bumped", False),
        }

    def fetch_list(self, pages: int | None = None) -> dict:
        """Fetch N pages with retries; returns a summary dict (never raises for page>0)."""
        pages = max(1, int(pages or self.cfg["pages"]))
        pages = min(pages, int(self.cfg["fetch"]["max_pages"]))
        collected: list[dict] = []
        page_errors: list[str] = []

        def attempt(_i):
            return self.fetch_page(0)

        def on_fail(attempt_no, delay, exc):
            self.store.add_failure(
                "fetch_list",
                str(exc),
                attempt=attempt_no,
                context={"page": 0, "next_delay_s": round(delay, 1)},
            )
            self.log(f"list page 0 attempt {attempt_no} failed: {exc} (retry in {delay:.0f}s)")

        ok, first, err = http_util.with_retries(
            attempt,
            attempts=int(self.cfg["fetch"]["attempts"]),
            schedule=list(self.cfg["fetch"]["backoff_s"]),
            jitter=float(self.cfg["fetch"]["jitter"]),
            on_attempt_failure=on_fail,
            abort_check=lambda: http_util.cooldown_remaining() > _COOLDOWN_ABORT_S,
        )
        if not ok:
            raise RuntimeError(f"list fetch failed after retries: {err}")

        collected.extend(first["topics"])
        for page in range(1, pages):
            if http_util.cooldown_remaining() > _COOLDOWN_ABORT_S:
                page_errors.append(f"page {page}: skipped, client cooling down")
                break
            try:
                time.sleep(1.5)
                collected.extend(self.fetch_page(page)["topics"])
            except Exception as exc:
                page_errors.append(f"page {page}: {exc}")
                self.store.add_failure("fetch_list_page", str(exc), context={"page": page})
                self.log(f"list page {page} failed (continuing): {exc}")
                break

        seen: dict[int, dict] = {}
        for topic in collected:
            seen[topic["id"]] = topic
        return {"topics": list(seen.values()), "pages": min(pages, 1 + len(collected) // 30), "page_errors": page_errors}

    # ----------------------------------------------------------------------- rss
    # The tag's RSS feed carries 30 items *with the opening post body* in a single
    # request - far cheaper than 30 paced detail calls, and much less likely to trip
    # the rate limiter. Topics the feed does not cover fall back to per-topic detail.
    def rss_url(self) -> str:
        return self.cfg["fetch"].get("rss_url") or (self.cfg["tag_page_url"].rstrip("/") + ".rss")

    def fetch_rss_bodies(self) -> dict[int, dict]:
        if not self.cfg["fetch"].get("use_rss", True):
            return {}
        headers = dict(http_util.BROWSER_HEADERS)
        headers["accept"] = "application/rss+xml, application/xml;q=0.9, */*;q=0.8"
        status, raw, _ = http_util.get(self.rss_url(), headers=headers, timeout=self.cfg["request_timeout_s"])
        text = raw.decode("utf-8", "replace")
        cap = int(self.cfg["fetch"]["body_cap"])
        out: dict[int, dict] = {}
        for item in re.findall(r"<item>(.*?)</item>", text, re.S):
            link = re.search(r"<link>(.*?)</link>", item, re.S)
            if not link:
                continue
            match = re.search(r"/t/(?:topic|[\w-]+)/(\d+)", link.group(1))
            if not match:
                continue
            tid = int(match.group(1))
            desc = re.search(r"<description>(.*?)</description>", item, re.S)
            body = desc.group(1) if desc else ""
            body = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", body.strip(), flags=re.S)
            title = re.search(r"<title>(.*?)</title>", item, re.S)
            pub = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
            out[tid] = {
                "body_text": html_to_text(body)[:cap],
                "rss_title": html_mod.unescape(title.group(1)).strip() if title else "",
                "rss_pub_date": pub.group(1).strip() if pub else "",
            }
        return out

    def apply_rss_bodies(self, bodies: dict[int, dict]) -> dict:
        applied = matched = 0
        for tid, payload in bodies.items():
            topic = self.store.get(tid)
            if not topic:
                continue
            matched += 1
            if topic.get("detail_fetched"):
                continue
            body = payload.get("body_text") or ""
            self.store.set_fields(
                tid,
                body_text=body,
                detail_fetched=True,
                detail_source="rss",
            )
            applied += 1
        return {"rss_items": len(bodies), "rss_matched": matched, "rss_applied": applied}

    # -------------------------------------------------------------------- detail
    def detail_direct(self, tid: int) -> dict:
        blob = http_util.get_json(
            f"{self.cfg['site_url']}/t/topic/{tid}.json",
            headers=linuxdo_headers(),
            timeout=self.cfg["request_timeout_s"],
        )
        return self._body_from_topic_json(blob)

    def detail_jina(self, tid: int) -> dict:
        prefix = self.cfg["fetch"]["jina_prefix"]
        url = f"{prefix}{self.cfg['site_url']}/t/topic/{tid}.json"
        status, raw, _ = http_util.get(url, headers=http_util.JINA_HEADERS, timeout=60)
        blob = extract_json_blob(raw.decode("utf-8", "replace"))
        return self._body_from_topic_json(blob)

    def _body_from_topic_json(self, blob: dict) -> dict:
        posts = (blob.get("post_stream") or {}).get("posts") or []
        op = posts[0] if posts else {}
        body = html_to_text(op.get("cooked") or "")
        cap = int(self.cfg["fetch"]["body_cap"])
        return {
            "body_text": body[:cap],
            "title": html_mod.unescape(blob.get("title") or "").strip(),
            "author": op.get("username") or "",
            "created_at": op.get("created_at"),
            "reply_count": blob.get("reply_count"),
            "views": blob.get("views"),
            "like_count": blob.get("like_count"),
        }

    def ensure_details(self, topic_ids: list[int]) -> dict:
        """Fetch OP bodies for topics that need them. Paced; jina as fallback.

        Stops early when linux.do keeps refusing (consecutive failures or a long
        rate-limit cooldown): the remaining bodies are simply retried next cycle.
        """
        cfg_fetch = self.cfg["fetch"]
        budget = int(cfg_fetch.get("detail_max_per_cycle", 40))
        max_consecutive = int(cfg_fetch.get("detail_max_consecutive_failures", 5))
        ok = failed = via_jina = deferred = 0
        consecutive_failures = 0
        direct_challenges = 0
        # After a couple of challenges the direct path is clearly rate-limited for now:
        # stop paying its 8s penalty and go straight to the proxy for the rest.
        skip_direct_after = int(cfg_fetch.get("skip_direct_after_challenges", 2))
        skip_direct = False
        targets = topic_ids[:budget]
        for index, tid in enumerate(targets):
            topic = self.store.get(tid)
            if not topic or topic.get("detail_fetched"):
                continue
            detail = None
            direct_err = None
            if not skip_direct:
                try:
                    detail = self.detail_direct(tid)
                    direct_challenges = 0
                except Exception as exc:
                    direct_err = exc
                    if getattr(exc, "is_challenge", False):
                        direct_challenges += 1
                        # Cloudflare rate-limits the detail path intermittently; a short
                        # pause and one retry recovers most of these without the proxy.
                        self.log(f"detail {tid} challenged ({exc}); retrying direct after wait")
                        time.sleep(float(cfg_fetch.get("challenge_wait_s", 8)))
                        try:
                            detail = self.detail_direct(tid)
                            direct_challenges = 0
                        except Exception as exc2:
                            direct_err = exc2
                            direct_challenges += 1
                            self.store.add_failure(
                                "detail", str(exc2), context={"topic_id": tid, "via": "direct_retry"}
                            )
                        if direct_challenges >= skip_direct_after:
                            skip_direct = True
                            self.log(
                                f"direct detail path rate-limited ({direct_challenges} challenges); "
                                "using the proxy for the rest of this cycle"
                            )
                    else:
                        self.store.add_failure("detail", str(exc), context={"topic_id": tid, "via": "direct"})
            if detail is None:
                time.sleep(float(cfg_fetch.get("jina_delay_s", 4.0)))
                try:
                    detail = self.detail_jina(tid)
                    via_jina += 1
                except Exception as exc:
                    failed += 1
                    consecutive_failures += 1
                    self.store.add_failure(
                        "detail",
                        f"{exc}",
                        context={"topic_id": tid, "via": "jina", "direct_error": str(direct_err)[:200]},
                    )
                    self.log(f"detail {tid} failed (direct+jina): {exc}")
                    cooling = http_util.cooldown_remaining() > _COOLDOWN_ABORT_S
                    if consecutive_failures >= max_consecutive or cooling:
                        deferred = len(targets) - index - 1
                        self.store.add_failure(
                            "detail_deferred",
                            f"stopped detail phase after {consecutive_failures} consecutive failures",
                            context={"deferred": deferred, "cooling_down": cooling},
                        )
                        self.log(f"detail phase stopped early; {deferred} bodies deferred to the next cycle")
                        break
                    time.sleep(float(cfg_fetch.get("detail_delay_s", 2.2)))
                    continue
            self.store.set_fields(
                tid,
                body_text=detail.get("body_text") or "",
                detail_fetched=True,
                detail_source="jina" if via_jina and detail else "direct",
            )
            ok += 1
            consecutive_failures = 0
            if ok % int(cfg_fetch.get("save_every", 5)) == 0:
                self.store.save()  # survive a kill mid-phase
            time.sleep(float(cfg_fetch.get("detail_delay_s", 2.2)))
        self.last_detail_source = {"direct": ok - via_jina, "jina": via_jina, "failed": failed}
        return {
            "detail_ok": ok,
            "detail_failed": failed,
            "detail_via_jina": via_jina,
            "detail_deferred": deferred,
        }
