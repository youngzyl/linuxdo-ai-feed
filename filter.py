"""The filter: judge topics with CommandCode's deepseek/deepseek-v4.1-flash.

Key comes from the environment (`commandcode_apikey`, see config.resolve_api_key).
The key is never logged and never included in any HTTP response.
"""
from __future__ import annotations

import json
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
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y", "有价值"}
    return False


def normalize_verdict(row: dict, model: str, prompt_version: str) -> dict:
    index = row.get("i", row.get("index", row.get("id")))
    try:
        index = int(index)
    except Exception:
        index = None
    valuable = None
    for key in ("valuable", "is_valuable", "keep", "picked", "value"):
        if key in row:
            valuable = _coerce_bool(row[key])
            break
    score = row.get("score")
    try:
        score = int(float(score))
    except Exception:
        score = None
    if valuable is None and score is not None:
        valuable = score >= 60
    if valuable is None:
        valuable = False
    return {
        "i": index,
        "valuable": bool(valuable),
        "score": score,
        "category": str(row.get("category") or "").strip()[:24],
        "reason": str(row.get("reason") or "").strip()[:120],
        "summary": str(row.get("summary") or "").strip()[:220],
        "model": model,
        "prompt_version": prompt_version,
    }


class Filter:
    def __init__(self, cfg: dict, store, logger):
        self.cfg = cfg
        self.store = store
        self.log = logger
        self.last_error: str | None = None

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
    def judge_batch(self, key: str, batch: list[dict]) -> tuple[list[dict], list[dict]]:
        """Returns (verdicts, unjudged_topics)."""
        cfg = self.cfg["filter"]
        prompt = build_user_prompt(batch, int(cfg.get("body_chars", 1200)))
        text = self.call_model(key, [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}])
        rows = extract_results(text)
        by_index: dict[int, dict] = {}
        for row in rows:
            verdict = normalize_verdict(row, cfg["model"], cfg["prompt_version"])
            if verdict["i"]:
                by_index[verdict["i"]] = verdict
        verdicts: list[dict] = []
        unjudged: list[dict] = []
        for i, topic in enumerate(batch, start=1):
            verdict = by_index.get(i)
            if verdict is None:
                unjudged.append(topic)
                continue
            verdict = dict(verdict)
            verdict["topic_id"] = topic["id"]
            verdicts.append(verdict)
        return verdicts, unjudged

    def run(self, *, limit: int | None = None) -> dict:
        key, key_source = resolve_api_key()
        cfg = self.cfg["filter"]
        summary = {
            "ok": True,
            "model": cfg["model"],
            "key_present": bool(key),
            "key_source": key_source,
            "batches_ok": 0,
            "batches_failed": 0,
            "judged": 0,
            "unjudged": 0,
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
            try:
                verdicts, unjudged = self.judge_batch(key, batch)
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
            summary["batches_ok"] += 1
            summary["unjudged"] += len(unjudged)
            for verdict in verdicts:
                self.store.set_verdict(verdict["topic_id"], verdict)
                summary["judged"] += 1
                if verdict["valuable"]:
                    summary["picked"] += 1
            for topic in unjudged:
                self.store.add_failure(
                    "filter_missing_index", "model answer omitted this topic", context={"topic_id": topic["id"]}
                )
            time.sleep(1.0)
        self.store.save()
        return summary
