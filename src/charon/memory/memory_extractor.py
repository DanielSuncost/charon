"""Memory extraction — LLM-based fact extraction from conversation sessions.

Takes a conversation session (list of turns) and extracts structured facts
for storage in the semantic memory engine. Handles knowledge updates,
temporal markers, and static/dynamic classification.
"""
from __future__ import annotations

import json
import re
from typing import Any


# ── Extraction prompt ───────────────────────────────────────────────

EXTRACTION_SYSTEM = """You are a memory extraction system. You analyze conversation sessions
and extract ALL facts about the user worth remembering long-term.

Extract facts that are:
- About the user (preferences, biographical details, habits, activities, goals)
- Specific (contain names, numbers, dates, durations, concrete details)
- Mentioned even casually or in passing — these are often the most important
- About the user's life events, achievements, possessions, relationships

DO NOT extract:
- Generic advice the assistant gave
- Conversational filler ("thanks", "sure", etc.)
- Facts only about the assistant

Be thorough. If the user mentions something about themselves even once in passing
(e.g., "I graduated with a degree in X" or "my commute is 45 minutes"), extract it.
These incidental mentions are critical to remember."""

EXTRACTION_PROMPT = """Extract facts from this conversation session dated {session_date}.

Output a JSON array of facts. For each fact:
{{
  "content": "single clear sentence stating the fact",
  "category": "biographical|preference|event|knowledge|project|relationship",
  "is_static": true or false,
  "event_date": "YYYY-MM-DD" or null,
  "temporal_markers": ["before X", "after Y"] or [],
  "supersedes": "the OLD fact this updates/replaces" or null
}}

Categories:
- biographical: name, age, job, education, location, family
- preference: likes, dislikes, style choices, tool preferences
- event: something that happened (activities, achievements, experiences)
- knowledge: factual claims, project details, technical knowledge
- project: work projects, goals, deadlines
- relationship: people the user knows, social connections

Rules for is_static:
- true: permanent facts (name, degree, hometown) — unlikely to change
- false: current activities, recent events, ongoing projects — may change

Rules for supersedes:
- If the user mentions an update to something previously discussed, set
  supersedes to a description of what changed. Example:
  "I just beat my 5K personal best, now it's 25:50" → supersedes: "previous 5K personal best time"

Rules for temporal:
- Extract event_date when you can determine a specific date
- Use temporal_markers for relative time ("before my trip", "after graduation")

Session ({session_date}):
{session_text}

Output ONLY the JSON array, no other text:"""


def _format_session(session: list[dict], session_date: str = "") -> str:
    """Format a session into readable text."""
    lines = []
    if session_date:
        lines.append(f"[Date: {session_date}]")
    for turn in session:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict)]
            content = " ".join(parts)
        # Truncate very long turns
        if len(content) > 2000:
            content = content[:2000] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ── Extract facts (sync, using provider_bridge) ────────────────────

def extract_facts_from_session(
    session: list[dict],
    session_date: str = "",
    *,
    provider_call: Any = None,
    model: str | None = None,
) -> list[dict]:
    """Extract structured facts from a conversation session.

    Args:
        session: list of turns, each {"role": ..., "content": ...}
        session_date: date string for the session
        provider_call: async callable(messages, model) -> str
        model: model ID to use for extraction

    Returns:
        list of fact dicts ready for memory_engine.add()
    """
    session_text = _format_session(session, session_date)

    # Skip very short sessions
    if len(session_text) < 50:
        return []

    prompt = EXTRACTION_PROMPT.format(
        session_date=session_date or "unknown",
        session_text=session_text,
    )

    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    if provider_call is None:
        # Return empty if no provider — caller must supply one
        return []

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context — use create_task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                response = pool.submit(
                    asyncio.run,
                    provider_call(messages, model)
                ).result(timeout=60)
        else:
            response = asyncio.run(provider_call(messages, model))
    except Exception:
        try:
            import asyncio
            response = asyncio.run(provider_call(messages, model))
        except Exception:
            return []

    return parse_extraction_response(response)


def extract_facts_sync(
    session: list[dict],
    session_date: str = "",
    *,
    llm_call: Any = None,
) -> list[dict]:
    """Synchronous version — llm_call is a sync callable(messages) -> str."""
    session_text = _format_session(session, session_date)
    if len(session_text) < 50:
        return []

    prompt = EXTRACTION_PROMPT.format(
        session_date=session_date or "unknown",
        session_text=session_text,
    )

    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    if llm_call is None:
        return []

    try:
        response = llm_call(messages)
    except Exception:
        return []

    return parse_extraction_response(response)


# ── Parse LLM response ─────────────────────────────────────────────

def parse_extraction_response(response: str) -> list[dict]:
    """Parse the LLM's JSON response into fact dicts."""
    if not response:
        return []

    # Try to extract JSON array from response
    response = response.strip()

    # Handle markdown code blocks
    if "```" in response:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
        if match:
            response = match.group(1).strip()

    try:
        facts = json.loads(response)
    except json.JSONDecodeError:
        # Try to find a JSON array in the response
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if match:
            try:
                facts = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    if not isinstance(facts, list):
        return []

    # Validate and normalize
    valid_categories = {
        "biographical", "preference", "event", "knowledge", "project", "relationship"
    }
    normalized = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        content = fact.get("content", "").strip()
        if not content or len(content) < 5:
            continue

        category = fact.get("category", "general")
        if category not in valid_categories:
            category = "general"

        entry = {
            "content": content,
            "category": category,
            "is_static": bool(fact.get("is_static", False)),
        }

        if fact.get("event_date"):
            entry["event_date"] = str(fact["event_date"])

        if fact.get("forget_after"):
            entry["forget_after"] = str(fact["forget_after"])

        # Store supersedes info for the engine to process
        if fact.get("supersedes"):
            entry["_supersedes"] = str(fact["supersedes"])

        # Store temporal markers as metadata
        if fact.get("temporal_markers"):
            entry["_temporal_markers"] = fact["temporal_markers"]

        normalized.append(entry)

    return normalized


# ── Batch extraction for benchmark ──────────────────────────────────

def extract_all_sessions(
    sessions: list[list[dict]],
    dates: list[str],
    *,
    llm_call: Any = None,
) -> list[dict]:
    """Extract facts from all sessions in order. For benchmark use."""
    all_facts = []
    for session, date in zip(sessions, dates, strict=False):
        facts = extract_facts_sync(session, date, llm_call=llm_call)
        for fact in facts:
            fact["_session_date"] = date
        all_facts.extend(facts)
    return all_facts
