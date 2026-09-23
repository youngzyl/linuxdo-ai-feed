"""Preference learning from explicit user feedback.

The filter itself is a fixed prompt; this module is the learning loop around it.
Three signals, all explicit (never inferred from a click or a hover):

  keep     — you marked a topic 纳入精选 (or rescued a rejected one). Positive example.
  skip     — you marked a pick 排除. Negative example.
  note     — optional one-line reason you typed (kept with the example).

An explicit vote is ONLY the manual override (`store.set_manual_override`), which owns
the display state. It is deliberately not tied to the third column: since the authorized
reader contract that column is 收藏 (bookmarks), written by the reader UI through
/api/queue, and a vote never adds or removes a bookmark.

The next filter cycle injects the most recent keep/skip examples into the user
prompt so the model can imitate *your* taste rather than the generic one. The
examples are stored in data/state.json under `feedback`.

Note where they go: the rendered examples (title, note, model verdict) are part of the
filter's user prompt, so they are sent to the **configured filter provider** whenever the
filter runs - the same provider your posts already go to. They are not shared with the
public research/Imagine providers, and the raw note text stays out of /api/state; only the
authenticated GET /api/feedback (owner token) returns it from this box.
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


def _apply_explicit_effects(store, topic: dict, vote: str) -> None:
    """The side effects of an explicit vote, deliberately kept in one place.

    The vote *is* the manual override (`store.set_manual_override`), and that is the
    whole effect: it is what owns the display state. 收藏 (bookmarks) are a separate,
    user-owned signal written by the reader UI through /api/queue, so a vote never adds
    or removes a bookmark (the coupling this function used to carry was removed with the
    authorized reader contract).
    """
    tid = int(topic["id"])
    was_picked = topic.get("state") == "picked"
    store.set_manual_override(tid, vote)
    if vote == "keep":
        if not was_picked:
            topic["rescued"] = True
    else:
        if was_picked:
            topic["skipped"] = True


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
        # An explicit vote beats any later model verdict: it is stored as the topic's
        # manual_override, and the display state is derived from it. The duel with the
        # next filter run also happens inside this same lock and is saved before the
        # caller can answer, so the response and the durable state cannot disagree.
        # Bookmarks are not part of this: they are written through /api/queue only.
        _apply_explicit_effects(store, topic, vote)
        store.add_feedback(entry)  # durable: state.json + append-only feedback.jsonl
        store.save()
    return entry


def remove(store, topic_id: int) -> None:
    """Clear the vote. The override goes with it, exposing the latest model verdict."""
    with store.lock:
        store.clear_feedback(int(topic_id))
        store.clear_manual_override(int(topic_id))
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
