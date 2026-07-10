"""Judge-loop driver — advances running judge loops from the daemon heartbeat.

judge_engine provides the primitives (run_baseline, run_iteration,
check_convergence) but is deliberately *not* self-driving: run_iteration's
docstring notes that "the actual implementation (code changes) must happen
BEFORE calling this". Nothing was wiring those primitives together, so a
created loop just sat in the store. This module is the missing driver.

It advances each running loop by a SINGLE step per call so the charon_loop
heartbeat stays responsive (one baseline or one iteration per tick rather than
blocking on a whole loop). The "implement" step is pluggable via an
`implementer` callable: tests inject a deterministic one; production uses a
scoped shade (shade_implementer) to make the change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# An implementer takes (config, working_dir) and is expected to make one
# focused change to the working tree, returning a short summary of what it did
# (or None / '' if it made no change).
Implementer = Callable[[object, Path], "str | None"]


def _finalize(config, conv) -> None:
    config.convergence = conv
    config.status = 'completed'
    config.completed_at = _now()
    config.updated_at = _now()


def advance_loop(state_dir: Path, config, *, implementer: Implementer,
                 working_dir: Path, checkpoint_mgr=None) -> tuple[object, dict]:
    """Advance one judge loop by a single step.

    Returns (config, event) where event describes what happened this tick. The
    config is persisted after each meaningful transition.
    """
    from charon.judge.judge_engine import (
        run_baseline, run_iteration, check_convergence, create_judge, save_loop,
    )

    if config.status not in ('created', 'running'):
        return config, {'action': 'skipped', 'status': config.status}

    judge = create_judge(config)

    # 1. Baseline (iteration 0) — measured once.
    if config.baseline is None:
        config = run_baseline(config, judge, working_dir, checkpoint_mgr=checkpoint_mgr)
        save_loop(state_dir, config)
        if config.status == 'failed':
            return config, {'action': 'baseline_failed'}
        return config, {'action': 'baseline', 'score': config.baseline}

    # 2. Already done? (e.g. budget/target reached on a previous tick)
    conv = check_convergence(config)
    if conv:
        _finalize(config, conv)
        save_loop(state_dir, config)
        return config, {'action': 'converged', 'reason': conv.reason,
                        'best_score': config.best_score}

    # 3. One iteration: implement the change, then score/keep/rollback.
    try:
        summary = implementer(config, working_dir)
    except Exception as e:
        summary = None
        impl_error = str(e)
    else:
        impl_error = ''

    if not summary:
        # The implementer produced nothing usable — treat as a failed step so a
        # stuck loop eventually converges on consecutive_failures.
        config.consecutive_failures += 1
        config.updated_at = _now()
        save_loop(state_dir, config)
        conv = check_convergence(config)
        if conv:
            _finalize(config, conv)
            save_loop(state_dir, config)
            return config, {'action': 'converged', 'reason': conv.reason,
                            'best_score': config.best_score}
        ev = {'action': 'implement_noop'}
        if impl_error:
            ev['error'] = impl_error
        return config, ev

    config, iteration = run_iteration(
        config, judge, working_dir,
        change_summary=str(summary)[:300],
        checkpoint_mgr=checkpoint_mgr,
    )
    save_loop(state_dir, config)

    event = {
        'action': 'iterated',
        'iteration': iteration.iteration,
        'score': iteration.score,
        'kept': iteration.kept,
        'status': iteration.status,
    }
    conv = check_convergence(config)
    if conv:
        _finalize(config, conv)
        save_loop(state_dir, config)
        event['converged'] = True
        event['reason'] = conv.reason
        event['best_score'] = config.best_score
    return config, event


def shade_implementer(state_dir: Path, config, working_dir: Path) -> "str | None":
    """Production implementer: spawn a scoped one-shot agent to make one change.

    Runs a ConversationEngine restricted to the loop's scope, feeding it the
    judge's iteration prompt (goal + latest feedback + program). Returns the
    agent's change summary, or None when no provider is configured / it fails.
    """
    import asyncio

    try:
        from charon.conversation.conversation_engine import ConversationEngine
        from charon.providers.model_registry import get_shade_provider_and_model
        from charon.judge.judge_engine import build_iteration_prompt
    except Exception:
        return None

    try:
        provider, model, ready = get_shade_provider_and_model(state_dir)
    except Exception:
        return None
    if not provider or not ready:
        return None

    prompt = build_iteration_prompt(config)
    engine = ConversationEngine(
        provider=provider,
        model=model,
        project_root=Path(working_dir),
        agent_name=f'judge-impl-{config.id}',
        system_prompt=(
            'You are an implementer inside an optimization loop. Make ONE '
            'focused change toward the goal using your tools, staying within '
            'the allowed scope, then stop and summarise what you changed.'
        ),
        state_dir=state_dir,
        max_tokens=16384,
    )
    # Enforce the loop's scope on file edits (empty scope = whole project),
    # and the frozen denylist (files that must not be modified).
    engine.scope = list(config.scope) if config.scope else None
    engine.frozen = list(config.frozen) if config.frozen else None

    text_parts: list[str] = []

    async def _run():
        async for ev in engine.submit(prompt):
            if ev.type == 'text_delta':
                text_parts.append(ev.data.get('text', ''))

    try:
        asyncio.run(_run())
    except Exception:
        return None

    summary = ''.join(text_parts).strip()
    return summary[:300] if summary else None


def tick_judge_loops(state_dir: Path, *, implementer: "Implementer | None" = None,
                     max_loops: int = 1) -> list[dict]:
    """Advance up to `max_loops` active judge loops by one step each.

    Intended to be called from the charon_loop heartbeat. Loops in 'created' or
    'running' status are advanced; 'paused'/'completed'/'failed' are skipped.
    """
    from charon.judge.judge_engine import list_loops, load_loop

    impl = implementer
    if impl is None:
        def impl(cfg, wd):
            return shade_implementer(state_dir, cfg, wd)

    events: list[dict] = []
    active = [lp for lp in list_loops(state_dir) if lp.get('status') in ('created', 'running')]

    for meta in active[:max_loops]:
        loop_id = meta.get('id')
        config = load_loop(state_dir, loop_id)
        if not config:
            continue
        working_dir = Path(config.project) if config.project else Path(state_dir)
        if not working_dir.exists():
            events.append({'loop_id': loop_id, 'action': 'skipped',
                           'reason': 'working_dir_missing'})
            continue

        checkpoint_mgr = None
        try:
            from charon.automation.checkpoint_manager import CheckpointManager
            # Whole-tree checkpoints (no scope): the loop needs byte-exact full
            # rollback and consistent frozen-path detection. The implementer's
            # write scope is enforced separately at the tool layer.
            checkpoint_mgr = CheckpointManager(state_dir, working_dir)
        except Exception:
            checkpoint_mgr = None

        try:
            _, ev = advance_loop(state_dir, config, implementer=impl,
                                 working_dir=working_dir, checkpoint_mgr=checkpoint_mgr)
        except Exception as e:
            ev = {'action': 'error', 'error': str(e)}
        ev['loop_id'] = loop_id
        events.append(ev)

    return events


__all__ = ['advance_loop', 'shade_implementer', 'tick_judge_loops']
