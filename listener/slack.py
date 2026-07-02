"""Slack alerts — one curated, ranked Block Kit message per run.

A run posts at most ONE message containing the threads that cleared the threshold,
ranked by composite score, each with its score breakdown and a link. If nothing
clears, we stay silent (no noise). Plain incoming-webhook — no Slack app to install.
"""

from __future__ import annotations
import json
import urllib.request

from .settings import settings


def _fire(score: int, pain: int) -> str:
    if score >= 85 or pain >= settings.hot_pain_floor:
        return "🔥 "
    return ""


def _card_block(item: dict) -> list[dict]:
    """item: {score, subreddit, age_hours, title, url, why, breakdown:{...}, trigger, action}"""
    b = item["breakdown"]
    age = item["age_hours"]
    age_str = f"{age:.0f}h" if age >= 1 else f"{int(age*60)}m"
    flag = _fire(item["score"], b.get("pain", 0))
    header = f"{flag}*{item['score']}* · r/{item['subreddit']} · {age_str} old"
    title = item["title"][:200].replace("\n", " ")
    breakdown = (
        f"📊 ICP {b.get('icp',0)} · Pain {b.get('pain',0)} · "
        f"Fresh {b.get('decay',0):.1f} · Promo {b.get('promo',0):.1f}"
    )
    if b.get("velocity") is not None:  # only present in OAuth mode
        breakdown += f" · Velocity {b['velocity']:.1f}/hr"
    meta = f"trigger: {item.get('trigger','none')} · suggested: {item.get('action','-')}"
    blocks = [
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"{header}\n*<{item['url']}|{title}>*"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": item.get("why", "")},
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": breakdown},
            {"type": "mrkdwn", "text": meta},
        ]},
    ]
    draft = item.get("draft")
    if draft:
        # code block = clean copy-paste; it's a DRAFT to review + post by hand
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"✍️ *Draft reply* — review, edit, post by hand:\n```{draft[:2800]}```",
        }})
    return blocks


def post_alerts(items: list[dict], scanned: int) -> bool:
    """items already filtered to >= threshold and sorted desc by score. Returns sent?"""
    if not items:
        return False
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text",
                 "text": f"🔎 Reddit signals — {len(items)} thread(s) worth your time"},
    }, {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"{scanned} new posts scanned this run"}],
    }]
    for it in items[:15]:  # cap card size
        blocks += _card_block(it)
    return _send({"blocks": blocks})


def post_error(message: str) -> None:
    _send({"text": f"⚠️ Reddit listener run issue: {message}"})


def _send(payload: dict) -> bool:
    if not settings.slack_webhook_url:
        print("[slack] no webhook configured; payload:\n", json.dumps(payload)[:800])
        return False
    try:
        req = urllib.request.Request(
            settings.slack_webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print(f"[slack] post failed: {e}")
        return False
