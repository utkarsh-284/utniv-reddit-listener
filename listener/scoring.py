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


def _providers() -> list[tuple[object, str, str]]:
    """Ordered list of (client, model, name): OpenAI first (user credits), NIM fallback."""
    from openai import OpenAI  # lazy import: keeps the deterministic core dependency-free
    out = []
    if settings.openai_api_key:
        out.append((OpenAI(api_key=settings.openai_api_key), settings.openai_model, "openai"))
    if settings.nim_api_key:
        out.append((OpenAI(api_key=settings.nim_api_key, base_url=settings.nim_base_url),
                    settings.nim_model, "nim"))
    return out


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
    for client, model, name in _providers():
        try:
            kwargs = dict(
                model=model, temperature=0.1, max_tokens=300,
                messages=[{"role": "system", "content": _SYS},
                          {"role": "user", "content": user}],
            )
            if name == "openai":  # gpt-4o-mini supports strict JSON mode
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
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
