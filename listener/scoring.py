"""Scoring = cheap deterministic math (free) + one LLM judgment call (OpenAI -> NIM).

Deterministic dims (velocity, decay, engagement, promo tolerance, phrase hits) are pure
arithmetic — no model needed. The LLM only judges what math can't: is this our ICP, how
acute is the pain, the trigger, and the one quotable line. Only posts that pass the free
phrase pre-filter ever reach the LLM, so cost stays at a few dollars a month.

Fail-open (engineering standards): if every LLM provider fails, the thread still gets a
deterministic-only score and is stored — the run never crashes and no data is lost.
"""

from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass

from .settings import settings
from config.phrases import match_phrases

# Thread is only needed for type hints; import lazily-safe to keep this module importable
# (and the deterministic core testable) even before praw/openai are installed.
try:
    from .reddit_client import Thread
except Exception:  # pragma: no cover
    Thread = object  # type: ignore

# ---------------------------------------------------------------- deterministic

def decay_factor(age_hours: float) -> float:
    if age_hours < 3:   return 1.0
    if age_hours < 12:  return 0.7
    if age_hours < 24:  return 0.3
    return 0.1


def deterministic(t: Thread) -> dict:
    age_hours = max((time.time() - t.created_utc) / 3600.0, 0.01)
    matched = match_phrases(f"{t.title}\n{t.body}")
    # upvotes/comments are None in RSS mode -> velocity/engagement unknown
    has_metrics = t.upvotes is not None and t.num_comments is not None
    velocity = (t.upvotes / age_hours) if has_metrics else None
    engagement = (t.num_comments / max(t.upvotes, 1)) if has_metrics else None
    return {
        "age_hours": round(age_hours, 2),
        "velocity": round(velocity, 3) if velocity is not None else None,
        "decay_factor": decay_factor(age_hours),
        "engagement": round(engagement, 3) if engagement is not None else None,
        "matched_phrases": matched,
        "phrase_hits": len(matched),
        "has_metrics": has_metrics,
        # normalized 0..100 for the composite (0 when unknown; weights drop them in RSS mode)
        "velocity_norm": (min(velocity / 20.0, 1.0) * 100) if velocity is not None else 0.0,
        "engagement_norm": (min(engagement / 2.0, 1.0) * 100) if engagement is not None else 0.0,
    }


# ---------------------------------------------------------------- LLM judgment

_SYS = """You triage Reddit threads for an agency knowledge-loss product (UTNIV). The ICP is
owners/operators of PR/advertising/creative/marketing agencies (and similar decision-heavy
teams) who lose client knowledge when people leave, never capture the "why" behind decisions,
have dead wikis, slow onboarding, or churn clients because "the people who got them left".

Given a thread's subreddit, title, and body, return STRICT JSON only:
{
  "icp_relevance": 0-100,        // is this our ICP talking about OUR problem? off-topic = low
  "pain_acuteness": 0-100,       // a live, costly, happening-now problem = high; abstract musing = low
  "is_question": true|false,     // are they asking something we could genuinely help with?
  "trigger_type": "departure|churn|onboarding|documentation|retrieval|notetaker|none",
  "voc_quote": "the single most quotable verbatim line from the post, or null",
  "suggested_action": "reply|mine|dm|ignore",
  "one_line_why": "<=15 words: why this thread matters, for a Slack card"
}
Rules: judge only what's written. Do not invent. Output JSON, nothing else."""


@dataclass
class LLMResult:
    icp_relevance: int = 0
    pain_acuteness: int = 0
    is_question: bool = False
    trigger_type: str = "none"
    voc_quote: str | None = None
    suggested_action: str = "ignore"
    one_line_why: str = ""
    provider: str | None = None


def _providers(openai_model: str | None = None, timeout: float = 20.0
               ) -> list[tuple[object, str, str]]:
    """Ordered (client, model, name) list. max_retries=0 + a short timeout so a dead/over-quota
    provider fails INSTANTLY and we fall through — never a multi-minute retry storm (which was
    blowing the CI timeout when OpenAI hit insufficient_quota). Order by LLM_PRIMARY.

    openai_model overrides the OpenAI model for THIS call set (scoring uses the cheap
    gpt-4o-mini default; drafting passes settings.draft_model so voice work runs on a stronger
    model). NIM keeps its own model as the fallback regardless."""
    from openai import OpenAI  # lazy import: keeps the deterministic core dependency-free
    openai_p = nim_p = None
    if settings.openai_api_key:
        openai_p = (OpenAI(api_key=settings.openai_api_key, max_retries=0, timeout=timeout),
                    openai_model or settings.openai_model, "openai")
    if settings.nim_api_key:
        nim_p = (OpenAI(api_key=settings.nim_api_key, base_url=settings.nim_base_url,
                        max_retries=0, timeout=timeout), settings.nim_model, "nim")
    order = [nim_p, openai_p] if settings.llm_primary == "nim" else [openai_p, nim_p]
    return [p for p in order if p]


def _is_reasoning(model: str) -> bool:
    """GPT-5.x and o-series are reasoning models: they REJECT `temperature` and `max_tokens`
    and require `max_completion_tokens` (+ optional `reasoning_effort`). Passing the legacy
    params 400s the call — which would silently fail over to NIM and tank draft quality."""
    m = (model or "").lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def _create(client, model: str, name: str, messages: list[dict], *,
            max_tokens: int, temperature: float | None = None, json_mode: bool = False):
    """One chat completion, mapping params to what the model actually accepts. Reasoning
    models (gpt-5.x) get max_completion_tokens and no temperature; classic models keep both."""
    kwargs: dict = {"model": model, "messages": messages}
    if _is_reasoning(model):
        kwargs["max_completion_tokens"] = max_tokens
        if settings.draft_reasoning_effort:   # empty -> omit -> model default (fast)
            kwargs["reasoning_effort"] = settings.draft_reasoning_effort
    else:
        kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
    if json_mode and name == "openai":  # OpenAI (4o + gpt-5.x) supports strict JSON mode
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs)


def _parse(raw: str) -> dict:
    raw = raw.strip()
    # tolerate models that wrap JSON in prose/fences
    if "```" in raw:
        raw = raw.split("```")[1].replace("json", "", 1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)


def llm_score(t: Thread) -> LLMResult:
    user = json.dumps({"subreddit": t.subreddit, "title": t.title, "body": t.body[:4000]})
    # scoring stays on the cheap, proven-accurate gpt-4o-mini (settings.openai_model)
    for client, model, name in _providers():
        try:
            resp = _create(client, model, name,
                           [{"role": "system", "content": _SYS},
                            {"role": "user", "content": user}],
                           max_tokens=300, temperature=0.1, json_mode=True)
            data = _parse(resp.choices[0].message.content or "")
            q = data.get("voc_quote")
            return LLMResult(
                icp_relevance=int(data.get("icp_relevance", 0)),
                pain_acuteness=int(data.get("pain_acuteness", 0)),
                is_question=bool(data.get("is_question", False)),
                trigger_type=str(data.get("trigger_type", "none")),
                voc_quote=(q.strip() if isinstance(q, str) and q.strip() else None),
                suggested_action=str(data.get("suggested_action", "ignore")),
                one_line_why=str(data.get("one_line_why", ""))[:160],
                provider=name,
            )
        except Exception as e:
            print(f"[scoring] {name} failed ({model}): {e}")
            continue
    return LLMResult(provider=None)  # fail-open: deterministic-only


# ---------------------------------------------------------------- composite

_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\butniv\.com\S*", re.IGNORECASE)
_PITCH_RE = re.compile(r"\b(utniv|scorecard|book a call|dm me|check out|reach out to me)\b", re.IGNORECASE)


def complete(system: str, user: str, max_tokens: int = 600, temperature: float = 0.6) -> str | None:
    """Generic voice completion for the weekly digest (authority + BIP posts). Runs on the
    stronger draft_model since this is voice work, not triage. Returns text or None."""
    for client, model, name in _providers(openai_model=settings.draft_model, timeout=30.0):
        try:
            resp = _create(client, model, name,
                           [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
                           max_tokens=max_tokens, temperature=temperature)
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            print(f"[complete] {name} failed: {e}")
            continue
    return None


def _sanitize(text: str) -> str:
    """Guardrails: no links / product mentions may ever ship in a draft."""
    text = _URL_RE.sub("", text or "")
    if _PITCH_RE.search(text):
        # drop any line that references the product/CTA
        text = "\n".join(ln for ln in text.splitlines() if not _PITCH_RE.search(ln))
    return text.strip()


def draft_reply(t: Thread, llm: LLMResult) -> dict | None:
    """Draft Reddit engagement in Utkarsh's voice, Mom-Test grounded. Returns
    {comment, if_they_reply: [..], dm_opener} — comment ready to paste, two deeper
    follow-up digs for when the OP responds, and a DM opener for landing a conversation.
    Gated: returned for Slack review, never auto-posted. Strips any link/pitch that slips in."""
    from config.reddit_voice import system_prompt, angle_for, SHAPES
    user = json.dumps({
        "subreddit": t.subreddit, "title": t.title, "body": t.body[:4000],
        "why_it_matters": llm.one_line_why, "trigger": llm.trigger_type,
    })
    angle = angle_for(llm.trigger_type)
    if angle:
        user += ("\n\nWhat's usually really going on in threads like this (context, don't "
                 "recite it): " + angle["read"]
                 + "\n\nMom-Test questions that fit this situation — pick at most ONE, and "
                 "reshape it in this thread's own words. Never paste any of these verbatim:\n"
                 + "\n".join(f"- {q}" for q in angle["asks"]))
    # rotate the comment's shape by thread id (stable across runs, unlike hash()) so
    # consecutive drafts never share an opener
    import zlib
    shape = SHAPES[zlib.crc32(t.reddit_id.encode()) % len(SHAPES)]
    user += "\n\nShape for THIS comment (mandatory): " + shape
    for client, model, name in _providers(openai_model=settings.draft_model, timeout=30.0):
        try:
            resp = _create(client, model, name,
                           [{"role": "system", "content": system_prompt()},
                            {"role": "user", "content": user}],
                           max_tokens=550, temperature=0.8, json_mode=True)
            raw = (resp.choices[0].message.content or "").strip()
            if not raw:
                continue
            try:
                data = _parse(raw)
            except Exception:
                # model ignored the JSON contract — salvage the text as the comment
                data = {"comment": raw.strip().strip('"')}
            comment = _sanitize(str(data.get("comment") or ""))
            if not comment:
                continue
            followups = [_sanitize(str(q)) for q in (data.get("if_they_reply") or [])
                         if str(q).strip()][:2]
            dm = data.get("dm_opener")
            dm = _sanitize(str(dm)) if isinstance(dm, str) and dm.strip() else None
            return {"comment": comment, "if_they_reply": followups, "dm_opener": dm}
        except Exception as e:
            print(f"[draft] {name} failed: {e}")
            continue
    return None


def suggest_posts(items: list[dict], sub_names: list[str]) -> list[dict]:
    """One LLM call per alerting run: 2 Mom-Test discussion-post ideas for the monitored
    subs, grounded in what people were actually posting about this run. Returns
    [{subreddit, title, body, learns}]. Fail-open: any error -> []."""
    from config.reddit_voice import SUGGEST_POSTS_SYS, PERSONAL_SAMPLES
    signals = [{"subreddit": it["subreddit"], "title": it["title"],
                "trigger": it.get("trigger"), "why": it.get("why")} for it in items[:8]]
    sys_prompt = SUGGEST_POSTS_SYS
    if PERSONAL_SAMPLES.strip():
        sys_prompt += ("\n\n# Write as this specific person (their voice, translated to Reddit)\n"
                       + PERSONAL_SAMPLES.strip())
    user = json.dumps({"monitored_subreddits": sub_names, "live_signals_this_run": signals})
    for client, model, name in _providers(openai_model=settings.draft_model, timeout=30.0):
        try:
            resp = _create(client, model, name,
                           [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user}],
                           max_tokens=700, temperature=0.7, json_mode=True)
            data = _parse(resp.choices[0].message.content or "")
            out = []
            for idea in (data.get("ideas") or [])[:3]:
                title = _sanitize(str(idea.get("title") or ""))
                body = _sanitize(str(idea.get("body") or ""))
                sub = str(idea.get("subreddit") or "").lstrip("r/").strip()
                if title and body and sub:
                    out.append({"subreddit": sub, "title": title, "body": body,
                                "learns": str(idea.get("learns") or "")[:120]})
            if out:
                return out
        except Exception as e:
            print(f"[suggest] {name} failed: {e}")
            continue
    return []


def composite(det: dict, llm: LLMResult) -> int:
    s = settings
    promo100 = det.get("promo_tolerance", 0.2) * 100
    if det.get("has_metrics"):
        score = (
            s.w_icp * llm.icp_relevance
            + s.w_pain * llm.pain_acuteness
            + s.w_decay * (det["decay_factor"] * 100)
            + s.w_velocity * det["velocity_norm"]
            + s.w_engagement * det["engagement_norm"]
            + s.w_promo * promo100
        )
    else:
        # RSS mode: no upvote/comment data -> lean on ICP + pain + freshness (re-normalized)
        score = (
            0.45 * llm.icp_relevance
            + 0.30 * llm.pain_acuteness
            + 0.15 * (det["decay_factor"] * 100)
            + 0.10 * promo100
        )
    return int(round(score))
