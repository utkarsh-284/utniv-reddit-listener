"""Supabase persistence (shared project, reddit_* tables). Service-role key, server-only.

Dedup is the `reddit_id` unique constraint: new threads insert with full LLM scoring;
already-seen threads upsert their live counts (so velocity tracks growth) WITHOUT paying
for the LLM again.
"""

from __future__ import annotations
from datetime import datetime, timezone

from .settings import settings


def client():
    from supabase import create_client  # lazy: dry-run / tests don't need supabase installed
    return create_client(settings.supabase_url, settings.supabase_service_key)


def active_subreddits(sb: Client) -> list[dict]:
    res = sb.table("reddit_subreddits").select("*").eq("active", True).execute()
    return res.data or []


def known_ids(sb: Client, reddit_ids: list[str]) -> set[str]:
    """Which of these reddit_ids already exist (so we don't re-score with the LLM)."""
    if not reddit_ids:
        return set()
    found: set[str] = set()
    for i in range(0, len(reddit_ids), 100):  # chunk the IN() filter
        chunk = reddit_ids[i:i + 100]
        res = sb.table("reddit_threads").select("reddit_id").in_("reddit_id", chunk).execute()
        found.update(r["reddit_id"] for r in (res.data or []))
    return found


def _iso(utc_seconds: float) -> str:
    return datetime.fromtimestamp(utc_seconds, tz=timezone.utc).isoformat()


def insert_thread(sb: Client, row: dict) -> str | None:
    res = sb.table("reddit_threads").insert(row).execute()
    return (res.data or [{}])[0].get("id")


def touch_thread(sb: Client, reddit_id: str, upvotes: int, num_comments: int,
                 velocity: float, composite_score: int) -> None:
    """Refresh live counts for an already-seen thread (no LLM)."""
    sb.table("reddit_threads").update({
        "upvotes": upvotes, "num_comments": num_comments,
        "velocity": velocity, "composite_score": composite_score,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }).eq("reddit_id", reddit_id).execute()


def get_unalerted(sb: Client) -> list[dict]:
    res = sb.table("reddit_threads").select("*").eq("alerted", False).execute()
    return res.data or []


def mark_alerted(sb: Client, thread_uuids: list[str]) -> None:
    if not thread_uuids:
        return
    sb.table("reddit_threads").update({"alerted": True, "status": "alerted"}) \
        .in_("id", thread_uuids).execute()


def save_voc(sb: Client, thread_uuid: str, quote: str, theme: str,
             subreddit: str, url: str) -> None:
    sb.table("reddit_voc").insert({
        "thread_id": thread_uuid, "quote": quote, "theme": theme,
        "subreddit": subreddit, "source_url": url,
    }).execute()


_ALL = "00000000-0000-0000-0000-000000000000"  # sentinel: id != this matches every real row


def reset(sb) -> None:
    """One-time cleanup: clear scored threads + run logs (keeps the subreddit config).
    reddit_voc / reddit_drafts cascade from reddit_threads."""
    sb.table("reddit_threads").delete().neq("id", _ALL).execute()
    sb.table("reddit_runs").delete().neq("id", _ALL).execute()


def start_run(sb: Client) -> str:
    res = sb.table("reddit_runs").insert({"ok": True}).execute()
    return (res.data or [{}])[0].get("id")


# Only these keys are real columns on reddit_runs. Runtime-only metrics in `stats`
# (posts_known, fetch_failed, …) are printed/returned but must not be sent to the DB,
# or the update fails with a missing-column error.
_RUN_COLS = {
    "subs_polled", "posts_fetched", "posts_new", "posts_scored", "alerts_sent",
    "llm_calls", "llm_provider", "errors", "ok", "finished_at",
}


def finish_run(sb: Client, run_id: str, stats: dict) -> None:
    payload = {k: v for k, v in stats.items() if k in _RUN_COLS}
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("reddit_runs").update(payload).eq("id", run_id).execute()
