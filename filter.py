"""The filter: judge topics with CommandCode's deepseek/deepseek-v4.1-flash.

Key comes from the environment (`commandcode_apikey`, see config.resolve_api_key).
The key is never logged and never included in any HTTP response.
"""
from __future__ import annotations

import json
import math
import re
import time

import http_util
from config import redact, resolve_api_key
from store import now_iso

SYSTEM_PROMPT = """你是 linux.do「人工智能」板块的信息筛选器。读者是资深技术用户，只看真正有信息量的重大内容。

判为有价值：
- 模型 / 产品的发布、上线、重大更新（版本号、价格、额度、可用性变化）
- API、工具、开源项目的发布或重要版本，包含获取方式与可复现的细节
- 技术深度内容：架构分析、逆向、实测评测、性能或成本对比、踩坑经验
- 安全事故、隐私事件、封号风控、监管与政策变化
- 行业重要动态：公司或团队重大变动、诉讼、并购、路线图变化

判为无价值：
- 羊毛、优惠、抽奖、推广、中转站广告、邀请码
- 纯水、闲聊、情绪帖、没有信息量的吐槽或提问
- 入门求助（怎么装、怎么用、报错怎么办）
- 个人日常、账号买卖、重复提问

尺度宁缺毋滥：只有当你认为「错过它会错过信息」时才判为有价值。score 0-100，>= 60 才算有价值。
reason 一句话说明值得或不值得的原因；summary 用中文概括核心信息，保留关键数字、版本号和结论。

只输出 JSON，不要任何解释或 markdown 代码块：
{"results":[{"i":1,"valuable":true,"score":82,"category":"模型发布","reason":"三十字以内","summary":"八十字以内"}]}
每个输入条目都要有一条结果，i 与输入编号一致。"""


class AuthError(RuntimeError):
    """Key missing / rejected - needs human attention, not a retry loop."""


def build_user_prompt(batch: list[dict], body_chars: int = 1200) -> str:
    blocks = []
    for i, topic in enumerate(batch, start=1):
        body = (topic.get("body_text") or topic.get("excerpt") or "").strip()
        body = re.sub(r"\s+", " ", body)[:body_chars]
        blocks.append(
            "\n".join(
                [
                    f"[{i}] 标题: {topic.get('title') or ''}",
                    f"    标签: {', '.join(topic.get('tags') or []) or '-'} | 分类: {topic.get('category') or '-'}"
                    f" | 回复: {topic.get('reply_count') or 0} | 浏览: {topic.get('views') or 0}"
                    f" | 点赞: {topic.get('like_count') or 0} | 时间: {(topic.get('created_at') or '')[:16]}",
                    f"    正文: {body or '(未取到正文，仅凭标题判断)'}",
                ]
            )
        )
    return "请筛选以下帖子：\n\n" + "\n\n".join(blocks)


def build_user_prompt_with_taste(batch: list[dict], examples_text: str, body_chars: int = 1200) -> str:
    body = build_user_prompt(batch, body_chars)
    if examples_text:
        return examples_text + "\n\n" + body
    return body


def extract_results(text: str) -> list[dict]:
    """Robustly pull the results array out of a model answer."""
    if not text:
        raise ValueError("empty model answer")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.M).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            blob = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        rows = blob
        if isinstance(blob, dict):
            for key in ("results", "items", "data", "result"):
                if isinstance(blob.get(key), list):
                    rows = blob[key]
                    break
            else:
                rows = [blob]
        if isinstance(rows, list) and rows:
            out = []
            for row in rows:
                if isinstance(row, dict):
                    out.append(row)
            if out:
                return out
    raise ValueError(f"cannot parse model answer as JSON (head: {cleaned[:160]!r})")


def _coerce_bool(value) -> bool:
    """Legacy permissive coercion, used only by `normalize_verdict` (compatibility)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y", "有价值"}
    return False


# Documented policy (SYSTEM_PROMPT): score 0-100, >= 60 counts as valuable.
SCORE_MIN = 0.0
SCORE_MAX = 100.0
VALUABLE_MIN_SCORE = 60
VALUABLE_KEYS = ("valuable", "is_valuable", "keep", "picked", "value")
TEXT_LIMITS = {"category": 24, "reason": 120, "summary": 220}


def _parse_index(value, *, strict: bool):
    """Indices are integers. A bool is an int in Python but is never an index."""
    if isinstance(value, bool):
        return None if strict else int(value)
    if isinstance(value, int):
        return value
    if strict:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _parse_score(value, *, strict: bool):
    """A finite number inside the documented policy range, else None."""
    if value is None:
        return None
    if strict:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number) or number < SCORE_MIN or number > SCORE_MAX:
            return None
        return int(number) if number.is_integer() else number
    try:
        return int(float(value))
    except Exception:
        return None


def _parse_valuable(row: dict, *, strict: bool):
    """(value, source). source: 'bool' (a real bool), 'type' (present, wrong type), 'missing'."""
    for key in VALUABLE_KEYS:
        if key in row:
            raw = row[key]
            if isinstance(raw, bool):
                return raw, "bool"
            if strict:
                return None, "type"
            return _coerce_bool(raw), "bool"
    return None, "missing"


def _parse_text(row: dict, key: str, *, strict: bool, counters: dict | None = None) -> str:
    """Trimmed, capped text. A container in a text field is dropped, never stringified."""
    value = row.get(key)
    limit = TEXT_LIMITS[key]
    if value is None:
        return ""
    if strict and not isinstance(value, str):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)[:limit]
        if counters is not None:
            counters["text_type_rejected"] = int(counters.get("text_type_rejected") or 0) + 1
        return ""
    return str(value).strip()[:limit]


def normalize_result_row(row, model: str, prompt_version: str, *, strict: bool = True, counters: dict | None = None):
    """One model row -> (verdict | None, reason).

    reason is 'ok' | 'bad_index' | 'bad_valuable' | 'bad_score'. In strict mode a wrong
    scalar type is a refusal, not something to coerce: a verdict has to be typed the way
    the policy says, otherwise the topic stays eligible for the next batch.
    """
    if not isinstance(row, dict):
        return None, "bad_index"
    index = _parse_index(row.get("i", row.get("index", row.get("id"))), strict=strict)
    if index is None and strict:
        return None, "bad_index"
    if strict:
        valuable, source = _parse_valuable(row, strict=True)
        if source == "type":
            return None, "bad_valuable"
        score = _parse_score(row.get("score"), strict=True)
        if row.get("score") is not None and score is None:
            return None, "bad_score"
        if source == "missing":
            if score is None:
                return None, "bad_valuable"  # no bool and no usable score: not a verdict
            valuable = score >= VALUABLE_MIN_SCORE  # documented policy
    else:
        valuable, _ = _parse_valuable(row, strict=False)
        score = _parse_score(row.get("score"), strict=False)
        if valuable is None and score is not None:
            valuable = score >= VALUABLE_MIN_SCORE
        if valuable is None:
            valuable = False
    return {
        "i": index,
        "valuable": bool(valuable),
        "score": score,
        "category": _parse_text(row, "category", strict=strict, counters=counters),
        "reason": _parse_text(row, "reason", strict=strict, counters=counters),
        "summary": _parse_text(row, "summary", strict=strict, counters=counters),
        "model": model,
        "prompt_version": prompt_version,
    }, "ok"


def normalize_verdict(row: dict, model: str, prompt_version: str) -> dict:
    """Legacy permissive helper (string coercion), kept for existing callers and tests.

    The batch path uses `normalize_result_row`/`normalize_batch`, which refuse wrong
    scalar types instead of coercing them.
    """
    verdict, _ = normalize_result_row(row, model, prompt_version, strict=False)
    return verdict


def normalize_batch(
    rows: list[dict],
    batch: list[dict],
    model: str,
    prompt_version: str,
    *,
    snapshot: dict | None = None,
) -> dict:
    """Map model rows onto the dispatched batch, strictly and accountably.

    Returns {"verdicts", "unjudged", "unjudged_reasons", "counts"}.

    * A row is accepted only when its index is an integer inside the batch and every
      scalar it carries is well typed.
    * A duplicated index makes that index ambiguous: the whole index is refused. There is
      no silent last-wins.
    * Unknown / out-of-range indices, malformed rows and missing indices are counted, and
      every topic without an accepted verdict stays eligible for the next cycle.
    """
    snapshot = snapshot or {}
    valid: dict[int, dict] = {}
    ambiguous: set[int] = set()
    invalid_reasons: dict[str, int] = {}
    counts = {
        "returned": 0,
        "accepted": 0,
        "missing": 0,
        "invalid": 0,
        "ambiguous": 0,
        "unknown": 0,
        "text_type_rejected": 0,
    }
    for row in rows or []:
        counts["returned"] += 1
        if not isinstance(row, dict):
            counts["invalid"] += 1
            invalid_reasons["not_object"] = invalid_reasons.get("not_object", 0) + 1
            continue
        verdict, reason = normalize_result_row(row, model, prompt_version, counters=counts)
        if reason != "ok":
            counts["invalid"] += 1
            invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
            continue
        index = verdict["i"]
        if index < 1 or index > len(batch):
            counts["unknown"] += 1
            continue
        if index in ambiguous:
            continue
        if index in valid:
            valid.pop(index, None)
            ambiguous.add(index)
            continue
        valid[index] = verdict

    verdicts: list[dict] = []
    unjudged: list[dict] = []
    unjudged_reasons: dict[int, str] = {}
    for i, topic in enumerate(batch, start=1):
        verdict = valid.get(i)
        tid = int(topic["id"])
        if verdict is None:
            unjudged.append(topic)
            if i in ambiguous:
                unjudged_reasons[tid] = "ambiguous"
            else:
                unjudged_reasons[tid] = "missing"
                counts["missing"] += 1
            continue
        item = dict(verdict)
        item["topic_id"] = topic["id"]
        item["source_version"] = snapshot.get(tid) or topic.get("source_version")
        verdicts.append(item)
    counts["accepted"] = len(verdicts)
    counts["ambiguous"] = len(ambiguous)
    counts["invalid_reasons"] = invalid_reasons
    return {"verdicts": verdicts, "unjudged": unjudged, "unjudged_reasons": unjudged_reasons, "counts": counts}


class Filter:
    def __init__(self, cfg: dict, store, logger):
        self.cfg = cfg
        self.store = store
        self.log = logger
        self.last_error: str | None = None
        # normalization detail (counts, per-topic reasons) of the most recent batch
        self.last_normalization: dict | None = None

    # ------------------------------------------------------------------ transport
    def _post_chat(self, key: str, messages: list[dict], *, use_json_mode: bool = True) -> str:
        cfg = self.cfg["filter"]
        body: dict = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg.get("temperature", 0),
            "stream": False,
        }
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "linuxdo-ai-feed/1.0",
        }
        if cfg.get("zdr"):
            headers["x-cmd-zdr"] = "1"
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        status, raw, _ = http_util.request(
            url,
            method="POST",
            headers=headers,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=cfg.get("timeout_s", 180),
        )
        blob = json.loads(raw.decode("utf-8", "replace"))
        return blob["choices"][0]["message"]["content"] or ""

    def call_model(self, key: str, messages: list[dict]) -> str:
        cfg = self.cfg["filter"]
        last: Exception | None = None

        def attempt(_i):
            return self._post_chat(key, messages)

        def on_fail(attempt_no, delay, exc):
            if isinstance(exc, http_util.HttpError) and exc.status in (400,) and "response_format" in str(exc.body_head).lower():
                raise_next = None  # handled below by flag retry
                raise_next
            self.store.add_failure(
                "filter_call",
                str(exc),
                attempt=attempt_no,
                context={"model": cfg["model"], "next_delay_s": round(delay, 1)},
            )
            self.log(f"filter call attempt {attempt_no} failed: {exc} (retry in {delay:.0f}s)")

        try:
            ok, text, err = http_util.with_retries(
                attempt,
                attempts=int(cfg.get("attempts", 3)),
                schedule=list(cfg.get("backoff_s", [4, 12, 30])),
                jitter=0.2,
                on_attempt_failure=on_fail,
            )
        except Exception as exc:
            raise
        if ok:
            return text
        # a provider that rejects response_format gets one clean retry without it
        if isinstance(err, http_util.HttpError) and err.status == 400:
            self.log("retrying without response_format json mode")
            return self._post_chat(key, messages, use_json_mode=False)
        status = getattr(err, "status", None)
        if status in (401, 403):
            raise AuthError(f"CommandCode rejected the key (HTTP {status}): {redact(str(err))[:200]}")
        raise RuntimeError(f"filter call failed: {err}")

    # ---------------------------------------------------------------------- judge
    def judge_batch(self, key: str, batch: list[dict], *, snapshot: dict | None = None) -> tuple[list[dict], list[dict]]:
        """Returns (verdicts, unjudged_topics).

        `snapshot` is the source version captured when this batch was dispatched
        ({topic_id: version}); it travels with each verdict so a late answer cannot be
        applied to bytes the model never saw.
        """
        cfg = self.cfg["filter"]
        taste = ""
        try:
            import learn as learn_mod

            taste = learn_mod.render_examples(learn_mod.examples_for_prompt(self.store))
        except Exception:
            taste = ""
        prompt = build_user_prompt_with_taste(batch, taste, int(cfg.get("body_chars", 1200)))
        text = self.call_model(key, [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}])
        rows = extract_results(text)
        result = normalize_batch(rows, batch, cfg["model"], cfg["prompt_version"], snapshot=snapshot)
        self.last_normalization = result
        return result["verdicts"], result["unjudged"]

    def run(self, *, limit: int | None = None) -> dict:
        key, key_source = resolve_api_key()
        cfg = self.cfg["filter"]
        summary = {
            "ok": True,
            "model": cfg["model"],
            "key_present": bool(key),
            "key_source": key_source,
            "batches_ok": 0,
            "batches_partial": 0,
            "batches_processed": 0,
            "batches_failed": 0,
            "judged": 0,
            "unjudged": 0,
            "missing": 0,
            "invalid": 0,
            "ambiguous": 0,
            "unknown": 0,
            "stale": 0,
            "partial": False,
            "picked": 0,
            "error": None,
            "finished_at": now_iso(),
        }
        pending = self.store.pending(limit=limit)
        if not pending:
            summary["ok"] = True
            return summary
        if not key:
            summary.update(ok=False, batches_failed=0, error="no API key (env commandcode_apikey)")
            self.last_error = summary["error"]
            return summary

        batch_size = int(cfg.get("batch_size", 8))
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            # immutable dispatch snapshot: versions as of the moment this batch is sent
            snapshot = {int(t["id"]): t.get("source_version") for t in batch}
            try:
                verdicts, unjudged = self.judge_batch(key, batch, snapshot=snapshot)
            except AuthError as exc:
                summary.update(ok=False, batches_failed=summary["batches_failed"] + 1, error=str(exc))
                self.last_error = str(exc)
                self.store.add_failure("filter_auth", str(exc), context={"model": cfg["model"]})
                break
            except Exception as exc:
                summary.update(ok=False, batches_failed=summary["batches_failed"] + 1, error=str(exc))
                self.last_error = str(exc)
                self.store.add_failure("filter_batch", str(exc), context={"model": cfg["model"], "size": len(batch)})
                continue
            summary["batches_processed"] += 1
            counts = (self.last_normalization or {}).get("counts") or {}
            for name in ("missing", "invalid", "ambiguous", "unknown"):
                summary[name] += int(counts.get(name) or 0)
            batch_gaps = sum(int(counts.get(name) or 0) for name in ("missing", "invalid", "ambiguous", "unknown"))
            if batch_gaps:
                summary["batches_partial"] += 1
            else:
                summary["batches_ok"] += 1
            summary["unjudged"] += len(unjudged)
            for verdict in verdicts:
                status = self.store.set_verdict(
                    verdict["topic_id"], verdict, source_version=verdict.get("source_version")
                )
                if status == "stale":
                    # the bytes moved while the answer was in flight: the topic keeps its
                    # current version and stays eligible, so nothing is lost by refusing
                    summary["stale"] += 1
                    self.store.add_failure(
                        "filter_stale_verdict",
                        "model answer arrived for an older source version",
                        context={
                            "topic_id": verdict["topic_id"],
                            "judged_version": verdict.get("source_version"),
                            "current_version": (self.store.get(verdict["topic_id"]) or {}).get("source_version"),
                        },
                    )
                    continue
                summary["judged"] += 1
                if verdict["valuable"]:
                    summary["picked"] += 1
            reasons = (self.last_normalization or {}).get("unjudged_reasons") or {}
            for topic in unjudged:
                reason = reasons.get(int(topic["id"]), "missing")
                stage = "filter_ambiguous_index" if reason == "ambiguous" else "filter_missing_index"
                self.store.add_failure(stage, f"model answer {reason} for this topic", context={"topic_id": topic["id"]})
            if counts.get("invalid") or counts.get("unknown"):
                self.store.add_failure(
                    "filter_invalid_result",
                    "model answer contained rows that cannot be attributed to a batch item",
                    context={
                        "invalid": int(counts.get("invalid") or 0),
                        "unknown": int(counts.get("unknown") or 0),
                        "reasons": counts.get("invalid_reasons") or {},
                        "text_type_rejected": int(counts.get("text_type_rejected") or 0),
                    },
                )
            time.sleep(1.0)
        gaps_total = summary["missing"] + summary["invalid"] + summary["ambiguous"] + summary["unknown"]
        summary["partial"] = bool(gaps_total)
        if summary["batches_failed"]:
            # a hard call failure already recorded the streak; keep it
            summary["ok"] = False
        elif summary["partial"]:
            # an accepted batch with holes is not a full success: the summary says so and
            # the caller must not reset the filter failure streak
            summary["ok"] = False
            summary["error"] = (
                f"partial filter result: {summary['invalid']} invalid, {summary['ambiguous']} ambiguous, "
                f"{summary['missing']} missing, {summary['unknown']} unknown"
            )
        elif summary["batches_processed"] and not summary["judged"]:
            # every batch came back but nothing landed: not a full success either
            summary["ok"] = False
            summary["error"] = summary.get("error") or "no verdict landed on the current source version"
        self.store.save()
        return summary
