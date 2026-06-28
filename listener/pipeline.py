"""Orchestrates one run: fetch -> dedup -> pre-filter -> score -> store -> alert.

The whole run is wrapped so one failure can't lose data or block the next cron tick
(fail-open). Every run writes a row to reddit_runs for observability + cost tracking.
"""

from __future__ import annotations
import traceback

from .settings import settings
from . import reddit_client, scoring, store, slack
from config.phrases import match_phrases


def dry_run() -> dict:
    """Fetch + gate only. No Supabase, no Slack, no LLM, no spend — verifies intake + the gate."""
    subs = [  # busy group (sort 10-21)
            "marketing", "digital_marketing", "advertising", "socialmedia", "SEO", "PPC",
            "webdev", "consulting", "sysadmin", "devops", "projectmanagement", "humanresources",
            # niche group (sort 30-41)
            "agency", "agencylife", "PublicRelations", "content_marketing", "branding",
            "web_design", "bigseo", "freelance", "msp", "CustomerSuccess",
            "ProductManagement", "managementconsulting"]
    threads = reddit_client.fetch_all(subs)
    passed = [t for t in threads if match_phrases(f"{t.title}\n{t.body}")]
    print(f"\n[dry-run] fetched {len(threads)} posts; {len(passed)} pass the gate "
          f"(would be LLM-scored). No writes, no Slack, no spend.\n")
    for t in passed[:25]:
        terms = match_phrases(f"{t.title}\n{t.body}")
        print(f"  • [{t.subreddit}] {t.title[:62]!r}  <- {terms[:4]}")
    return {"ok": True, "fetched": len(threads), "gated": len(passed)}


def _alert_predicate(comp: int, pain: int, icp: int, action: str) -> bool:
    return (comp >= settings.alert_threshold
            or pain >= settings.hot_pain_floor
            or (action == "reply" and icp >= settings.reply_icp_floor))


def realert() -> dict:
    """Re-scan already-scored rows and push any that now qualify (e.g. after tuning the
    threshold or the reply rule). No refetch, no LLM cost — just reads the DB and alerts."""
    missing = settings.validate()
    if missing:
        print("[realert] missing config:", missing)
        return {"ok": False}
    sb = store.client()
    rows = store.get_unalerted(sb)
    items, uuids = [], []
    for r in rows:
        comp = r.get("composite_score") or 0
        pain = r.get("pain_acuteness") or 0
        icp = r.get("icp_relevance") or 0
        action = r.get("suggested_action") or ""
        if not _alert_predicate(comp, pain, icp, action):
            continue
        uuids.append(r["id"])
        items.append({
            "score": comp, "subreddit": r["subreddit"],
            "age_hours": float(r.get("age_hours") or 0),
            "title": r["title"], "url": r["url"],
            "why": r.get("one_line_why") or "", "trigger": r.get("trigger_type") or "none",
            "action": action or "-",
            "breakdown": {"icp": icp, "pain": pain,
                          "decay": float(r.get("decay_factor") or 0),
                          "velocity": r.get("velocity"), "promo": 0.2},
        })
    items.sort(key=lambda x: x["score"], reverse=True)
    sent = 0
    if items and slack.post_alerts(items, scanned=len(rows)):
        store.mark_alerted(sb, uuids)
        sent = len(items)
    print(f"[realert] {len(rows)} un-alerted rows scanned, {sent} alerted")
    return {"ok": True, "scanned": len(rows), "alerted": sent}


def run(dry: bool = False) -> dict:
    if dry:
        return dry_run()
    missing = settings.validate()
    if missing:
        msg = f"missing config: {', '.join(missing)}"
        print("[pipeline]", msg)
        return {"ok": False, "error": msg}

    sb = store.client()
    run_id = store.start_run(sb)
    stats = {"subs_polled": 0, "posts_fetched": 0, "posts_known": 0, "posts_new": 0,
             "posts_scored": 0, "alerts_sent": 0, "llm_calls": 0,
             "llm_provider": None, "errors": [], "ok": True}
    alert_items: list[dict] = []
    alert_uuids: list[str] = []

    try:
        # sort by sort_order so combined-feed groups stay busy-vs-niche (robust if column absent)
        subs = sorted(store.active_subreddits(sb),
                      key=lambda s: (s.get("sort_order") or 100, s["name"]))
        promo_by_sub = {s["name"]: float(s.get("promo_tolerance", 0.2)) for s in subs}
        names = [s["name"] for s in subs if s.get("poll_new", True)]
        stats["subs_polled"] = len(names)

        threads = reddit_client.fetch_all(names)
        stats["posts_fetched"] = len(threads)

        # Reliability guard: a total fetch failure (Reddit blocked the runner's IP) must be
        # visible, not silent. Warn to Slack, log it, and exit cleanly so the cron retries.
        if not threads:
            stats["fetch_failed"] = True
            stats["errors"].append("0 posts fetched — Reddit likely rate-limited/blocked this IP")
            slack.post_error("couldn't reach Reddit this run (likely a datacenter-IP rate limit). "
                             "Will retry next cycle. If this repeats, switch to the local runner.")
            store.finish_run(sb, run_id, stats)
            print("[pipeline] fetch failed (0 posts); warned Slack; exiting cleanly for retry.")
            return stats

        seen = store.known_ids(sb, [t.reddit_id for t in threads])

        for t in threads:
            det = scoring.deterministic(t)
            det["promo_tolerance"] = promo_by_sub.get(t.subreddit, 0.2)

            # already-seen: just refresh live counts (no LLM), then move on.
            if t.reddit_id in seen:
                stats["posts_known"] += 1
                store.touch_thread(sb, t.reddit_id, t.upvotes, t.num_comments,
                                   det["velocity"], 0)
                continue

            stats["posts_new"] += 1

            # free pre-filter: no signal terms -> skip WITHOUT storing, so if we broaden the
            # gate later these get re-evaluated (re-checking the gate is free; no LLM).
            if det["phrase_hits"] == 0:
                continue

            llm = scoring.llm_score(t)
            stats["posts_scored"] += 1
            if llm.provider:
                stats["llm_calls"] += 1
                stats["llm_provider"] = llm.provider

            comp = scoring.composite(det, llm)
            thread_uuid = store.insert_thread(sb, _row(t, det, llm, comp))

            # capture VoC quote regardless of alert threshold (it's all goldmine)
            if thread_uuid and llm.voc_quote:
                store.save_voc(sb, thread_uuid, llm.voc_quote, llm.trigger_type,
                              t.subreddit, t.url)

            # decide alert: composite over threshold, OR acute pain, OR the LLM itself said
            # "reply" (its direct worth-engaging verdict) with a light ICP-relevance guard.
            hot = llm.pain_acuteness >= settings.hot_pain_floor
            worth_reply = (llm.suggested_action == "reply"
                           and llm.icp_relevance >= settings.reply_icp_floor)
            if comp >= settings.alert_threshold or hot or worth_reply:
                alert_uuids.append(thread_uuid)
                alert_items.append({
                    "score": comp, "subreddit": t.subreddit, "age_hours": det["age_hours"],
                    "title": t.title, "url": t.url,
                    "why": llm.one_line_why, "trigger": llm.trigger_type,
                    "action": llm.suggested_action,
                    "breakdown": {
                        "icp": llm.icp_relevance, "pain": llm.pain_acuteness,
                        "decay": det["decay_factor"], "velocity": det["velocity"],
                        "promo": det["promo_tolerance"],
                    },
                })

        alert_items.sort(key=lambda x: x["score"], reverse=True)
        if alert_items:
            if slack.post_alerts(alert_items, scanned=stats["posts_new"]):
                stats["alerts_sent"] = len(alert_items)
                store.mark_alerted(sb, [u for u in alert_uuids if u])

    except Exception as e:
        stats["ok"] = False
        stats["errors"].append(str(e))
        print("[pipeline] FATAL:", e)
        traceback.print_exc()
        try:
            slack.post_error(str(e))
        except Exception:
            pass
    finally:
        store.finish_run(sb, run_id, stats)

    print(f"[pipeline] done: {stats}")
    if stats["ok"] and stats["posts_scored"] == 0:
        print(f"[pipeline] quiet run — {stats['posts_known']} already-seen, "
              f"{stats['posts_new']} new but none matched the gate. Nothing new to alert.")
    return stats


def _row(t, det, llm, comp, status="new") -> dict:
    from datetime import datetime, timezone
    return {
        "reddit_id": t.reddit_id, "subreddit": t.subreddit, "url": t.url,
        "permalink": t.permalink, "title": t.title, "body": t.body,
        "author": t.author,
        "created_utc": datetime.fromtimestamp(t.created_utc, tz=timezone.utc).isoformat(),
        "upvotes": t.upvotes, "num_comments": t.num_comments,
        "age_hours": det["age_hours"], "velocity": det["velocity"],
        "decay_factor": det["decay_factor"], "engagement": det["engagement"],
        "phrase_hits": det["phrase_hits"], "matched_phrases": det["matched_phrases"],
        "icp_relevance": llm.icp_relevance or None,
        "pain_acuteness": llm.pain_acuteness or None,
        "is_question": llm.is_question,
        "trigger_type": llm.trigger_type,
        "suggested_action": llm.suggested_action,
        "one_line_why": llm.one_line_why or None,
        "llm_scored": bool(llm.provider),
        "llm_model": llm.provider,
        "composite_score": comp,
        "status": status,
    }
