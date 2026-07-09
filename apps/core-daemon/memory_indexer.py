"""Memory indexer — indexes conversation turns into the semantic memory engine.

Called after each conversation session to build the searchable memory store.
This is a background operation — it never blocks the conversation.

Non-breaking: if sqlite-vec or sentence-transformers are not installed,
this module does nothing.

Two indexing modes:
  index_conversation()         — fast, no LLM, embeds raw turns verbatim
  extract_and_index_facts()    — slow, LLM-based, extracts structured facts
                                  (category, is_static, event_date, supersedes)
                                  and stores them with full version tracking.
                                  Skips short/trivial sessions automatically.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path


def _index_in_background(state_dir: Path, turns: list[dict], agent_id: str, conv_id: str):
    """Index conversation turns into the memory engine. Runs in a thread."""
    try:
        from memory_engine import MemoryEngine

        engine = MemoryEngine(state_dir)
        for i, turn in enumerate(turns):
            content = turn.get('content', '')
            if isinstance(content, list):
                parts = [b.get('text', '') for b in content if isinstance(b, dict)]
                content = ' '.join(parts)
            if not isinstance(content, str) or len(content) < 30:
                continue

            # Index user and assistant turns (both contain useful info)
            engine.add(
                content[:2000],
                category='event',
                container_tag=agent_id,
                source_agent=agent_id,
                source_conv=conv_id,
                source_turn=i,
                check_updates=False,  # fast path — skip version detection during indexing
            )

        engine.close()
    except ImportError:
        pass  # sqlite-vec or sentence-transformers not installed
    except Exception:
        pass  # don't crash the main loop on indexing errors


def index_conversation(
    state_dir: Path,
    turns: list[dict],
    agent_id: str = "",
    conv_id: str = "",
) -> None:
    """Index conversation turns into the semantic memory engine.

    Non-blocking — spawns a background thread.
    Safe to call even if dependencies are missing.
    """
    if not turns:
        return

    thread = threading.Thread(
        target=_index_in_background,
        args=(state_dir, turns, agent_id, conv_id),
        daemon=True,
    )
    thread.start()


def _extract_in_background(state_dir: Path, turns: list[dict], agent_id: str, conv_id: str):
    """LLM-based fact extraction — runs in a background thread after task completion.

    Uses memory_extractor to send the conversation to the configured LLM and
    extract structured facts (category, is_static, event_date, supersedes).
    Results are stored in MemoryEngine with full version tracking (check_updates=True).

    Skips if:
    - session is too short (< 3 turns or < 200 chars total)
    - provider is not configured
    - memory_extractor or memory_engine deps are missing
    """
    try:
        # Gate: skip trivial sessions
        total_chars = sum(len(str(t.get('content', ''))) for t in turns)
        if len(turns) < 3 or total_chars < 200:
            return

        from memory_extractor import extract_facts_sync
        from memory_engine import MemoryEngine
        from provider_bridge import create_provider_and_model

        provider, model, ready = create_provider_and_model(state_dir)
        if not ready:
            return

        # Build a sync LLM callable that wraps the async provider.stream
        def llm_call(messages: list[dict]) -> str:
            text_parts: list[str] = []

            async def _run() -> str:
                async for delta in provider.stream(
                    messages=messages,
                    model=model,
                    system_prompt='You are a memory extraction system. Output only valid JSON.',
                    max_tokens=2048,
                ):
                    if hasattr(delta, 'type') and delta.type == 'text':
                        text_parts.append(delta.text)
                return ''.join(text_parts)

            try:
                return asyncio.run(_run())
            except Exception:
                return ''

        session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        facts = extract_facts_sync(turns, session_date, llm_call=llm_call)

        if not facts:
            return

        engine = MemoryEngine(state_dir)
        for fact in facts:
            content = fact.get('content', '').strip()
            if not content or len(content) < 5:
                continue
            engine.add(
                content,
                category=fact.get('category', 'general'),
                tier='user',
                container_tag=agent_id,
                is_static=bool(fact.get('is_static', False)),
                event_date=fact.get('event_date'),
                source_agent=agent_id,
                source_conv=conv_id,
                check_updates=True,  # enables version tracking / supersedes detection
            )
        engine.close()

    except ImportError:
        pass
    except Exception:
        pass


def extract_and_index_facts(
    state_dir: Path,
    turns: list[dict],
    agent_id: str = "",
    conv_id: str = "",
) -> None:
    """Extract structured facts from a conversation and store in semantic memory.

    LLM-based, non-blocking — spawns a background thread.
    Complements index_conversation() which does fast verbatim embedding.
    Safe to call even if provider or deps are missing.
    """
    if not turns:
        return

    thread = threading.Thread(
        target=_extract_in_background,
        args=(state_dir, turns, agent_id, conv_id),
        daemon=True,
    )
    thread.start()


def index_conversation_sync(
    state_dir: Path,
    turns: list[dict],
    agent_id: str = "",
    conv_id: str = "",
) -> int:
    """Synchronous version — returns count of indexed memories. For testing."""
    try:
        from memory_engine import MemoryEngine

        engine = MemoryEngine(state_dir)
        count = 0
        for i, turn in enumerate(turns):
            content = turn.get('content', '')
            if isinstance(content, list):
                parts = [b.get('text', '') for b in content if isinstance(b, dict)]
                content = ' '.join(parts)
            if not isinstance(content, str) or len(content) < 30:
                continue

            engine.add(
                content[:2000],
                category='event',
                container_tag=agent_id,
                source_agent=agent_id,
                source_conv=conv_id,
                source_turn=i,
                check_updates=False,
            )
            count += 1

        engine.close()
        return count
    except ImportError:
        return 0
    except Exception:
        return 0
