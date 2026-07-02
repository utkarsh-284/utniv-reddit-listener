"""Weekly Authority + Intelligence engine.

Turns the week's listening data into (1) an intelligence brief — trending pains + the sharpest
voice-of-customer quotes + top threads (feeds the dossier, copywriting, and pitch-sharpening),
and (2) drafted authority + build-in-public posts in Utkarsh's voice, grounded in what agencies
actually said this week. Posts to Slack; drafts are stored + posted BY HAND (never auto-posted).

Run: python run.py --digest   (weekly cron in .github/workflows/reddit-digest.yml)
"""

from __future__ import annotations
import collections
import json

from .settings import settings
from . import store, slack, scoring
from config.reddit_voice import DOSSIER_HOOKS, post_system_prompt

_TRIGGER_LABEL = {
    "departure": "senior departures", "churn": "client churn / continuity",
    "onboarding": "onboarding / ramp", "documentation": "dead wikis / documentation",
    "retrieval": "hunting for context", "notetaker": "notetakers / unread transcripts",
    "none": "other",
}


def gather(sb, days: int = 7) -> dict:
    threads = store.threads_since(sb, days)
    voc = store.voc_since(sb, days)
    runs = store.runs_since(sb, days)

    triggers = collections.Counter(
        t.get("trigger_type") or "none" for t in threads if (t.get("icp_relevance") or 0) >= 40)
    top_threads = [t for t in threads if (t.get("composite_score") or 0) >= settings.alert_threshold][:8]
    # best quotes: longest-ish, de-duped, cap 6
    seen, quotes = set(), []
    for q in sorted(voc, key=lambda x: len(x.get("quote") or ""), reverse=True):
        text = (q.get("quote") or "").strip()
        if text and text not in seen:
            seen.add(text)
            quotes.append(q)
        if len(quotes) >= 6:
            break

    return {
        "days": days,
        "n_scored": len(threads),
        "n_scanned": sum(r.get("posts_new") or 0 for r in runs),
        "n_alerts": sum(r.get("alerts_sent") or 0 for r in runs),
        "triggers": triggers,
        "top_threads": top_threads,
        "quotes": quotes,
    }


def brief_blocks(s: dict) -> list[dict]:
    top_trends = ", ".join(f"{_TRIGGER_LABEL.get(k,k)} ({n})" for k, n in s["triggers"].most_common(5)) or "—"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🧠 Weekly Reddit Intelligence"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"last {s['days']} days · {s['n_scanned']} new posts scanned · "
                    f"{s['n_scored']} scored · {s['n_alerts']} alerted"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Trending pains:* {top_trends}"}},
    ]
    if s["quotes"]:
        qtext = "\n".join(f"> _{(q.get('quote') or '')[:220]}_  · r/{q.get('subreddit','?')}"
                          for q in s["quotes"][:5])
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Voice-of-customer this week:*\n" + qtext}})
    if s["top_threads"]:
        ttext = "\n".join(f"• *{t.get('composite_score')}* <{t.get('url')}|{(t.get('title') or '')[:70]}>"
                          f" · r/{t.get('subreddit','?')}" for t in s["top_threads"][:6])
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Top threads:*\n" + ttext}})
    return blocks


def _grounding_text(s: dict) -> str:
    trends = ", ".join(f"{_TRIGGER_LABEL.get(k,k)}" for k, _ in s["triggers"].most_common(3)) or "knowledge loss"
    quotes = "\n".join(f"- \"{(q.get('quote') or '')[:200]}\"" for q in s["quotes"][:4]) or "(none captured)"
    return (f"This week's trending pains among agencies: {trends}.\n"
            f"Real things agency people said this week (their words):\n{quotes}\n\n"
            f"Sourced truths you may draw on (never invent new numbers):\n{DOSSIER_HOOKS}")


def _split_title_body(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    parts = text.split("\n", 1)
    title = parts[0].strip().lstrip("#").strip().strip('"')
    body = (parts[1].strip() if len(parts) > 1 else "")
    # guardrail: no links slip into posts
    body = scoring._URL_RE.sub("", body)
    return title, body


def draft_posts(s: dict) -> list[dict]:
    grounding = _grounding_text(s)
    out = []
    # 1 authority post (Motion A) anchored on the top trend
    a = scoring.complete(post_system_prompt("authority"),
                         grounding + "\n\nWrite ONE authority discussion post anchored on the single "
                         "strongest trend above.", max_tokens=550, temperature=0.6)
    if a:
        t, b = _split_title_body(a)
        out.append({"kind": "authority_post", "subreddit": "r/agency", "title": t,
                    "body": b, "grounded_on": ", ".join(k for k, _ in s["triggers"].most_common(2))})
    # 1 build-in-public post (Motion B)
    bip = scoring.complete(post_system_prompt("bip"),
                           grounding + "\n\nWrite ONE build-in-public post about what a week of "
                           "listening to these subs taught you. Honest, no fabricated metrics.",
                           max_tokens=550, temperature=0.6)
    if bip:
        t, b = _split_title_body(bip)
        out.append({"kind": "bip_post", "subreddit": "r/buildinpublic", "title": t,
                    "body": b, "grounded_on": "week of listening"})
    return out


def post_blocks_for_drafts(drafts: list[dict]) -> list[dict]:
    blocks = [{"type": "divider"},
              {"type": "section", "text": {"type": "mrkdwn",
               "text": "*✍️ Draft posts for this week* — review, edit, post by hand:"}}]
    for d in drafts:
        label = "Authority post" if d["kind"] == "authority_post" else "Build-in-public post"
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{label}* — suggested: {d['subreddit']}\n*{d['title']}*\n```{d['body'][:2600]}```"}})
    return blocks


def weekly_digest(dry: bool = False) -> dict:
    missing = settings.validate()
    if missing:
        print("[digest] missing config:", missing)
        return {"ok": False}
    sb = store.client()
    s = gather(sb, days=7)
    drafts = draft_posts(s)

    if dry:
        print(json.dumps({k: (v if not isinstance(v, collections.Counter) else dict(v))
                          for k, v in s.items() if k != "top_threads" and k != "quotes"}, indent=2))
        print("\n--- DRAFTS ---")
        for d in drafts:
            print(f"\n[{d['kind']}] {d['subreddit']}\n{d['title']}\n{d['body']}")
        return {"ok": True, "dry": True, "drafts": len(drafts)}

    blocks = brief_blocks(s)
    if drafts:
        blocks += post_blocks_for_drafts(drafts)
    slack.post_blocks(blocks)
    for d in drafts:  # secondary — never let a storage hiccup lose the Slack brief
        try:
            store.save_content(sb, d["kind"], d["subreddit"], d["title"], d["body"], d["grounded_on"])
        except Exception as e:
            print(f"[digest] save_content skipped ({e}) — run db/0003_reddit_content.sql")
    print(f"[digest] posted brief + {len(drafts)} drafts")
    return {"ok": True, "drafts": len(drafts)}
