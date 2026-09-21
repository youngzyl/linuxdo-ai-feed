"""Preference learning from explicit user feedback.

The filter itself is a fixed prompt; this module is the learning loop around it.
Three signals, all explicit (never inferred from a click or a hover):

  keep     — you queued it / you rescued a rejected topic. Positive example.
  skip     — you marked a pick as not interesting. Negative example.
  note     — optional one-line reason you typed (kept with the example).

The next filter cycle injects the most recent keep/skip examples into the user
prompt so the model can imitate *your* taste rather than the generic one. The
examples are stored in data/state.json under `feedback` and never leave the box.
"""
from __future__ import annotations

from store import now_iso

VALID = {"keep", "skip"}
MAX_EXAMPLES = 12  # injected into a prompt; keep it short
MAX_STORED = 80


def _topic_snapshot(topic: dict | None) -> dict:
    if not topic:
        return {}
    filt = topic.get("filter") or {}
    return {
        "id": int(topic["id"]),
        "title": (topic.get("title") or "")[:80],
        "category": topic.get("category") or "",
        "tags": list(topic.get("tags") or [])[:4],
        "state_at_vote": topic.get("state"),
        "model_score": filt.get("score"),
        "model_category": filt.get("category"),
        "model_reason": (filt.get("reason") or "")[:80],
    }


def record(store, topic_id: int, vote: str, note: str = "") -> dict:
    """Record one vote. A later vote on the same topic replaces the earlier one."""
    vote = (vote or "").strip().lower()
    if vote not in VALID:
        raise ValueError(f"vote must be keep or skip, got {vote!r}")
    topic = store.get(topic_id)
    if not topic:
        raise KeyError(f"unknown topic {topic_id}")
    entry = {
        "id": int(topic_id),
        "vote": vote,
        "note": (note or "").strip()[:80],
        "at": now_iso(),
        **_topic_snapshot(topic),
    }
    with store.lock:
        # keep/skip also move the topic between columns so the UI reflects the vote
        # immediately, without waiting for the next filter cycle.
        if vote == "keep" and topic.get("state") != "picked":
            topic["state"] = "picked"
            topic["rescued"] = True
        if vote == "skip" and topic.get("state") == "picked":
            topic["state"] = "rejected"
            topic["skipped"] = True
            store.data["queue"] = [x for x in store.data["queue"] if x != int(topic_id)]
    store.add_feedback(entry)  # durable: state.json + append-only feedback.jsonl
    store.save()
    return entry


def remove(store, topic_id: int) -> None:
    store.clear_feedback(int(topic_id))
    store.save()


def all_votes(store) -> list[dict]:
    return store.feedback_rows()


def counts(store) -> dict:
    rows = all_votes(store)
    keep = sum(1 for r in rows if r.get("vote") == "keep")
    skip = sum(1 for r in rows if r.get("vote") == "skip")
    return {"keep": keep, "skip": skip, "total": len(rows)}


def examples_for_prompt(store, *, keep_n: int = 6, skip_n: int = 6) -> dict:
    """Newest-first, capped. Used by the filter to condition the next batch."""
    rows = list(reversed(all_votes(store)))
    keep, skip = [], []
    for row in rows:
        bucket = keep if row.get("vote") == "keep" else skip
        cap = keep_n if row.get("vote") == "keep" else skip_n
        if len(bucket) >= cap:
            continue
        bucket.append(row)
        if len(keep) >= keep_n and len(skip) >= skip_n:
            break
    return {"keep": keep, "skip": skip}


def render_examples(examples: dict) -> str:
    """Plain-text block injected into the user prompt. Empty string if nothing yet."""
    keep = examples.get("keep") or []
    skip = examples.get("skip") or []
    if not keep and not skip:
        return ""
    lines = ["读者口味（来自ta亲手标的例子，请对照着筛；跟这些例子冲突时以例子为准，不要用通用尺度盖过去）："]
    if keep:
        lines.append("他会读（keep）：")
        for i, row in enumerate(keep, start=1):
            extra = f"；他的备注：{row['note']}" if row.get("note") else ""
            was = "模型当时判了有价值" if row.get("state_at_vote") == "picked" else "模型当时漏掉了，他捞回来的"
            lines.append(f"  {i}. 「{row.get('title') or ''}」——{was}{extra}")
    if skip:
        lines.append("他不读（skip）：")
        for i, row in enumerate(skip, start=1):
            extra = f"；他的备注：{row['note']}" if row.get("note") else ""
            was = "模型当时判了有价值，他划掉了" if row.get("state_at_vote") == "picked" else "模型当时也判了没价值"
            lines.append(f"  {i}. 「{row.get('title') or ''}」——{was}{extra}")
    lines.append("请按上面的口味筛，而不是按「资深技术用户一般会看什么」。")
    return "\n".join(lines)
