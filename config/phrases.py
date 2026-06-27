"""Pre-filter terms for the free gate (and secondary search).

The gate is deliberately BROAD (high recall): a post passes if it mentions any signal term,
and the LLM then does the real relevance judgement. Exact multi-word phrase matching was too
brittle ("lost a client" never matches "lost our biggest client today"), so we match on
word-boundary TERMS — single words and short phrases — generously.

Matching is case-insensitive with word boundaries. The count of matched terms is also used as
a small scoring signal (phrase_hits).
"""

from __future__ import annotations
import re

# Broad signal vocabulary for the knowledge-loss / decision-loss problem in agencies + ops teams.
TERMS: list[str] = [
    # departure / turnover
    "turnover", "quit", "quitting", "resign", "resigned", "resigning", "resignation",
    "gave notice", "leaving", "left the", "departure", "departed", "attrition",
    "laid off", "layoff", "layoffs", "fired", "let go",
    # knowledge in heads / continuity
    "tribal knowledge", "institutional knowledge", "undocumented", "in her head",
    "in his head", "in their heads", "only one who", "single point of failure",
    "bus factor", "knowledge transfer", "brain drain",
    # documentation graveyard
    "documentation", "document everything", "wiki", "sop", "sops", "knowledge base",
    "notion", "confluence", "handover", "handoff", "offboarding",
    # onboarding / ramp
    "onboarding", "onboard", "ramp up", "up to speed", "new hire", "new hires",
    "get up to speed", "months to be useful",
    # decisions / context / precedent
    "decision", "decisions", "the why", "lost the context", "precedent", "rationale",
    # retrieval tax
    "searching for", "recreating", "reinventing", "re-answer", "can't find",
    "asking around",
    # client continuity / churn
    "churn", "churned", "retention", "lost a client", "lost our", "losing clients",
    "client left", "client ghost", "they don't get us", "inherited",
    # notetaker truth
    "notetaker", "fireflies", "otter", "transcript", "transcripts", "meeting notes",
]

# secondary Reddit search queries (used only if SEARCH is ever enabled)
SEARCH_QUERIES: list[str] = [
    "tribal knowledge", "account lead quit", "onboarding takes months",
    "single point of failure", "lost a client", "wiki nobody updates",
    "knowledge walked out", "documentation is a mess",
]

_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(t) for t in TERMS) + r")(?!\w)",
    re.IGNORECASE,
)


def match_phrases(text: str) -> list[str]:
    """Return the distinct signal terms present in `text` (case-insensitive, word-boundary)."""
    if not text:
        return []
    found = {m.group(1).lower() for m in _PATTERN.finditer(text)}
    return sorted(found)
