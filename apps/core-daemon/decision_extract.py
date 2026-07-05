"""Heuristic decision extraction from agent output (roadmap §4.1: automatic
decision capture — today's alternative to agents explicitly calling log_decision).

Finds sentences where the writer COMMITS to a choice ("we decided to…", "went
with…", "settled on…", "Decision: …") and extracts the decision (what) and, when
an explicit rationale clause exists, the why. Deliberately conservative: hedges,
questions, negations, hypotheticals, and third-party decisions are rejected —
a silently wrong auto-decision in the thread record is worse than a missing one.

Measured: scripts/exp_decision_extraction.py (labeled precision/recall corpus
with hard negatives). This is a pattern heuristic, not NLU — phrasings outside
the pattern list are missed, and the eval prints exactly which.
"""
from __future__ import annotations

import re

# Sentences that must never fire: interrogatives, hedges, hypotheticals,
# explicit negations / not-yet states, and in-progress deliberation.
_REJECT = re.compile(
    r"""(?ix)
    \? |
    \b(?:should|could|might|may|maybe|perhaps|would|wondering|
        option|options|alternatives?|
        consider(?:ing)?|leaning|propos(?:e|es|ed|al|ing)|
        if\s+we|if\s+i|
        haven'?t\s+(?:yet\s+)?decided|hasn'?t\s+(?:yet\s+)?decided|
        not\s+(?:yet\s+)?decided|no\s+decision|still\s+deciding|yet\s+to\s+decide|
        were\s+choosing|discuss|
        # negated commitments: extracting these with inverted polarity would
        # write a WRONG decision into the thread record — reject the sentence
        not\s+going\s+to|won'?t|never|not\s+to\s+(?:use|adopt|go)|
        (?:'re|are|am|is)\s+not)\b
    """
)

# Explicit decision-record prefixes: "Decision: X", "Final call: X", "Resolved: X".
_LABELED = re.compile(
    r"(?i)\b(?:decision(?:\s+made)?|final\s+call|resolved|verdict)\s*:\s*(?P<what>[^.;\n]+)"
)

# Committed decision verbs. Past-tense / contracted-future forms only — present
# habits ("we use X in production") and in-progress forms must not match.
_COMMIT = re.compile(
    r"""(?ix)
    \b(?P<verb>
        decided\s+(?:to\s+(?:go\s+with\s+|use\s+|adopt\s+)?|on\s+|against\s+) |
        settled\s+on\s+ |
        opted\s+(?:for|to)\s+ |
        chose\s+ | picked\s+ | selected\s+ |
        went\s+with\s+ | going\s+with\s+ |
        (?:let'?s|we'?ll|we\s+will|i'?ll)\s+(?:go\s+with|use|adopt|stick\s+with)\s+ |
        going\s+to\s+use\s+ |
        agreed\s+to\s+ |
        landed\s+on\s+ |
        (?:'re\s+|are\s+|am\s+)?adopting\s+ |
        standardiz(?:e|ing)\s+on\s+ |
        concluded\s+that\s+
    )
    (?P<what>[^.;\n]+)
    """
)

# The decision must be OURS: first-person / team subject before the verb, or the
# verb opens the sentence (imperative summary style: "Settled on X."). Rejects
# "the vendor decided…", "he decided…", "several teams opted…".
_OUR_SUBJECT = re.compile(r"(?i)\b(?:we|i|let'?s|the\s+team|team\s+has|it'?s\s+decided)\b")

# Idiom collisions that pass the verb pattern but are not decisions.
_IDIOMS = re.compile(r"(?i)^(?:apart|out|up|between|the\s+flow)\b")

_WHY = re.compile(
    r"(?i)[\s,;—–-]*\b(?:because|since|as(?=\s+(?:it|its|the|this|that|we|i))|"
    r"due\s+to|so\s+that)\b\s*(?P<why>[^.;\n]+)"
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# High-confidence forms (explicit record or unambiguous decision verb) map to the
# same importance log_decision uses (80); softer commitments land at 70 so
# downstream importance-aware ranking can tell them apart.
_STRONG = re.compile(r"(?i)^(?:decided|settled|concluded)")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t,–—-")


def extract_decisions(text: str, *, max_decisions: int = 3) -> list[dict]:
    """Committed decisions in `text`: [{what, why, evidence, importance}].
    Conservative by design — see module docstring; measured by
    scripts/exp_decision_extraction.py."""
    out: list[dict] = []
    seen: set[str] = set()
    for sentence in _SENT_SPLIT.split(text or ""):
        sentence = sentence.strip()
        if not sentence or _REJECT.search(sentence):
            continue

        m = _LABELED.search(sentence)
        importance = 80
        if m:
            what = m.group("what")
        else:
            m = _COMMIT.search(sentence)
            if not m:
                continue
            prefix = sentence[: m.start()]
            if prefix.strip() and not _OUR_SUBJECT.search(prefix):
                continue  # someone else's decision
            what = m.group("what")
            if _IDIOMS.match(what.strip()):
                continue
            importance = 80 if _STRONG.match(m.group("verb").strip()) else 70

        why = ""
        w = _WHY.search(sentence, m.end("what") - len(m.group("what")))
        if w:
            why = _clean(w.group("why"))
            wm = _WHY.search(what)  # strip the rationale clause out of the decision span
            if wm:
                what = what[: wm.start()]
        what = _clean(what)
        if not what or what.lower() in seen:
            continue
        seen.add(what.lower())
        out.append({"what": what, "why": why, "evidence": sentence,
                    "importance": importance})
        if len(out) >= max_decisions:
            break
    return out


__all__ = ["extract_decisions"]
