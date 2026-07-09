"""Reddit access.

Reddit fully gated new Data API app creation (no client_id/secret obtainable), and
Devvit can't read subreddits you don't moderate — so the engine reads the public,
no-auth **RSS feeds** instead (SOURCE_MODE=rss, the default).

RSS gives us: title, link, author, published time, subreddit, and the post body — enough
for phrase pre-filtering, LLM relevance/pain scoring, freshness, and VoC capture. It does
NOT give live upvote/comment counts, so velocity/engagement scoring is disabled in RSS mode
(the composite re-weights onto ICP + pain + freshness; see scoring.py).

Reddit rate-limits RSS aggressively (429 on bursts), so we throttle between subs and back
off on 429. If you ever get OAuth credentials, set SOURCE_MODE=oauth to use PRAW instead.
"""

from __future__ import annotations
import html
import random
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

from .settings import settings

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean_html(raw: str) -> str:
    """RSS post bodies arrive as escaped HTML with a 'submitted by … [link]' footer.
    Reduce to clean plain text — better for the LLM, the DB, and reusable VoC quotes."""
    if not raw:
        return ""
    raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)   # drop SC_OFF/SC_ON comment markers
    raw = _TAG.sub(" ", raw)                              # strip all HTML tags
    raw = html.unescape(raw)                              # decode &#39; etc.
    cut = raw.find("submitted by")                       # drop the reddit "submitted by … [link]" footer
    if cut != -1:
        raw = raw[:cut]
    return _WS.sub(" ", raw).strip()

_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


@dataclass
class Thread:
    reddit_id: str
    subreddit: str
    url: str
    permalink: str
    title: str
    body: str
    author: str
    created_utc: float
    upvotes: int | None      # None in RSS mode (not exposed by the feed)
    num_comments: int | None


# ---------------------------------------------------------------- RSS (default)

def _iso_to_epoch(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return datetime.now(timezone.utc).timestamp()


def _id_from_link(link: str) -> str:
    # .../comments/1ugheff/lost_our_biggest_client_today/  ->  t3_1ugheff
    parts = [p for p in link.split("/") if p]
    if "comments" in parts:
        i = parts.index("comments")
        if i + 1 < len(parts):
            return f"t3_{parts[i + 1]}"
    return f"t3_{abs(hash(link))}"


def _fetch_feed(group: list[str], limit: int, deadline: float) -> str | None:
    """One combined request for several subs: r/a+b+c/new/.rss — far fewer requests = no 429s.
    Each entry still carries its own subreddit (the <category term=...>). Never waits past the
    shared `deadline` (monotonic seconds) so the run can't hang past the CI job timeout."""
    label = "+".join(group)
    url = f"https://www.reddit.com/r/{label}/new/.rss?limit={limit}"
    delay = settings.rss_backoff_seconds
    jitter = settings.rss_jitter_seconds
    for attempt in range(settings.rss_max_retries + 1):
        if time.monotonic() >= deadline:
            print("[rss] fetch deadline reached; giving up on this group")
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 429 (rate limit) and 403 (datacenter-IP block) are both worth a patient retry
            if e.code in (429, 403) and attempt < settings.rss_max_retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = (float(ra) if (ra and ra.isdigit()) else delay) + random.uniform(0, jitter)
                if time.monotonic() + wait >= deadline:  # not enough budget to retry
                    print(f"[rss] {e.code}, no time left before deadline; giving up")
                    return None
                print(f"[rss] {e.code} on group (attempt {attempt+1}), waiting {wait:.1f}s")
                time.sleep(wait)
                delay *= 2
                continue
            print(f"[rss] group HTTP {e.code}: {label}")
            return None
        except Exception as e:
            wait = delay + random.uniform(0, jitter)
            if attempt < settings.rss_max_retries and time.monotonic() + wait < deadline:
                print(f"[rss] transient error ({e}); retry in {wait:.1f}s")
                time.sleep(wait)
                delay *= 2
                continue
            print(f"[rss] group failed ({label}): {e}")
            return None
    return None


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _parse_rss(xml_text: str) -> list[Thread]:
    out: list[Thread] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[rss] parse error: {e}")
        return out
    for e in root.findall("a:entry", _ATOM):
        link_el = e.find("a:link", _ATOM)
        link = link_el.get("href") if link_el is not None else ""
        if not link:
            continue
        cat = e.find("a:category", _ATOM)
        out.append(Thread(
            reddit_id=e.findtext("a:id", default=_id_from_link(link), namespaces=_ATOM) or _id_from_link(link),
            subreddit=(cat.get("term") if cat is not None else "").lstrip("r/"),
            url=link,
            permalink=link.replace("https://www.reddit.com", ""),
            title=e.findtext("a:title", default="", namespaces=_ATOM) or "",
            body=_clean_html(e.findtext("a:content", default="", namespaces=_ATOM) or "")[:8000],
            author=(e.findtext("a:author/a:name", default="", namespaces=_ATOM) or "").lstrip("/u/"),
            created_utc=_iso_to_epoch(e.findtext("a:published", default="", namespaces=_ATOM) or ""),
            upvotes=None,
            num_comments=None,
        ))
    return out


def fetch_rss(subreddits: list[str], limit: int, max_age_hours: float) -> list[Thread]:
    """Fetch in small groups (combined feeds) instead of one-request-per-sub. With ~8 subs
    and a group size of 4, that's 2 requests total — no rate-limiting, full coverage."""
    cutoff = time.time() - max_age_hours * 3600
    deadline = time.monotonic() + settings.rss_deadline_seconds
    out: list[Thread] = []
    groups = _chunk(subreddits, max(1, settings.rss_group_size))
    for i, group in enumerate(groups):
        if time.monotonic() >= deadline:
            print("[rss] deadline reached; returning what we have so far")
            break
        if i:  # jittered pause between the (few) group requests, bounded by the deadline
            pause = settings.rss_delay_seconds + random.uniform(0, settings.rss_jitter_seconds)
            time.sleep(max(0.0, min(pause, deadline - time.monotonic())))
        xml_text = _fetch_feed(group, settings.rss_combined_limit, deadline)
        if not xml_text:
            continue
        for t in _parse_rss(xml_text):
            if t.created_utc >= cutoff:
                out.append(t)
    return out


# ---------------------------------------------------------------- OAuth (if you ever get keys)

def _fetch_oauth(subreddits: list[str]) -> list[Thread]:
    import praw  # lazy: only needed if SOURCE_MODE=oauth
    r = praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        username=settings.reddit_username or None,
        password=settings.reddit_password or None,
        user_agent=settings.reddit_user_agent,
    )
    if not (settings.reddit_username and settings.reddit_password):
        r.read_only = True
    cutoff = time.time() - settings.max_age_hours * 3600
    out: list[Thread] = []
    for name in subreddits:
        try:
            for s in r.subreddit(name).new(limit=settings.fetch_limit):
                if float(s.created_utc) < cutoff:
                    continue
                out.append(Thread(
                    reddit_id=s.fullname, subreddit=str(s.subreddit.display_name),
                    url=f"https://www.reddit.com{s.permalink}", permalink=s.permalink,
                    title=s.title or "", body=(s.selftext or "")[:8000],
                    author=str(s.author) if s.author else "[deleted]",
                    created_utc=float(s.created_utc),
                    upvotes=int(s.score or 0), num_comments=int(s.num_comments or 0),
                ))
        except Exception as e:
            print(f"[oauth] r/{name} failed: {e}")
    return out


# ---------------------------------------------------------------- entry

def fetch_all(subreddits: list[str]) -> list[Thread]:
    if settings.source_mode == "oauth":
        threads = _fetch_oauth(subreddits)
    else:
        threads = fetch_rss(subreddits, settings.fetch_limit, settings.max_age_hours)
    seen, unique = set(), []
    for t in threads:
        if t.reddit_id in seen:
            continue
        seen.add(t.reddit_id)
        unique.append(t)
    return unique
