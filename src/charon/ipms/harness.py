"""IPMS conditions harness.

Runs one scripted trajectory per prefix backbone, checkpoints it, then fans
out the five experimental conditions from that single checkpoint so probe
responses are paired across conditions:

- ``no-swap``      probes on the prefix model from an in-memory fork
                   (baseline: no swap mechanics at all)
- ``swap-same``    through-checkpoint resume onto the same model
                   (isolates swap-mechanics noise; the IS ceiling)
- ``swap-diff``    through-checkpoint resume onto the suffix model
                   (the treatment)
- ``memory-off``   suffix model, charter kept, history dropped
                   (scaffold carries no accumulated state; the IS floor)
- ``scaffold-off`` suffix model, history kept, charter-stripped prompt
                   (ablation: what does the charter layer buy?)

Each probe runs on a fresh engine forked from the same post-swap state, so
probes never contaminate each other. Engines carry no state_dir/agent_id:
the lossless store, memory layers, and tools stay out of the loop — in v1
the identity state under test is exactly (charter prompt + conversation
history), which is what the checkpoint preserves.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charon.context.context_transfer import (
    apply_checkpoint_to_engine,
    create_checkpoint_from_engine,
)
from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import Message, ModelInfo

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


CONDITIONS = ('no-swap', 'swap-same', 'swap-diff', 'memory-off', 'scaffold-off')


class IpmsRunError(RuntimeError):
    """A trajectory or probe turn failed at the provider level."""


@dataclass
class Backbone:
    provider: Any
    model: ModelInfo
    name: str = ''

    def __post_init__(self):
        if not self.name:
            self.name = f'{self.model.provider}/{self.model.model_id}'


@dataclass
class Probe:
    id: str
    kind: str  # 'continuity' | 'consistency' | 'decision' | 'persona' | 'preference'
    text: str
    expected: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrajectorySpec:
    """A scripted trajectory plus its probe battery.

    ``system_prompt`` is the full scaffold (identity + charter);
    ``stripped_system_prompt`` is the scaffold-off variant.
    """
    id: str
    system_prompt: str
    stripped_system_prompt: str
    turns: list[str]
    probes: list[Probe]
    max_tokens: int = 1024


@dataclass
class ConditionResult:
    condition: str
    prefix_model: str
    suffix_model: str
    probe_responses: list[dict[str, Any]]
    usage: dict[str, int]
    error: str = ''


@dataclass
class PairResult:
    spec_id: str
    prefix: str
    suffix: str
    checkpoint_id: str
    transcript: list[dict[str, Any]]
    conditions: dict[str, ConditionResult]
    billing_mode: str
    started_at: str
    finished_at: str


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _make_engine(backbone: Backbone, system_prompt: str, max_tokens: int) -> ConversationEngine:
    engine = ConversationEngine(
        backbone.provider, backbone.model,
        system_prompt=system_prompt,
        thinking_level='off',
        auto_compact=False,
        max_tokens=max_tokens,
    )
    engine.tools = []
    return engine


async def _submit_checked(engine: ConversationEngine, text: str) -> tuple[str, dict[str, int]]:
    """Submit one turn; raise on provider errors instead of returning ''."""
    reply, events = await engine.submit_and_collect(text)
    usage = {'input_tokens': 0, 'output_tokens': 0}
    for ev in events:
        if ev.type == 'error':
            raise IpmsRunError(str(ev.data.get('error', 'provider error')))
        if ev.type == 'message_end':
            u = ev.data.get('usage') or {}
            usage['input_tokens'] += int(u.get('input_tokens', 0) or 0)
            usage['output_tokens'] += int(u.get('output_tokens', 0) or 0)
    return reply, usage


def _fork_messages(messages: list[Message]) -> list[Message]:
    return [
        Message(
            role=m.role, content=m.content, tool_calls=list(m.tool_calls),
            tool_call_id=m.tool_call_id, tool_name=m.tool_name,
            is_error=m.is_error, thinking=m.thinking, timestamp=m.timestamp,
        )
        for m in messages
    ]


def _apply_strict(engine: ConversationEngine, checkpoint: dict[str, Any], **kwargs) -> ConversationEngine:
    """Resume, refusing any generation-time scaffold drift.

    Tools, thinking level, and max_tokens must be identical across the swap
    boundary or the measurement confounds scaffold drift with identity drift.
    """
    apply_checkpoint_to_engine(engine, checkpoint, **kwargs)
    mismatches = (getattr(engine, 'resume_checkpoint', {}) or {}).get('scaffold_mismatches') or []
    if mismatches:
        raise IpmsRunError(f'scaffold drift at resume: {mismatches}')
    return engine


def _probe_engine_for_condition(
    condition: str,
    *,
    prefix: Backbone,
    suffix: Backbone,
    spec: TrajectorySpec,
    checkpoint: dict[str, Any],
    prefix_messages: list[Message],
) -> ConversationEngine:
    """Build a fresh probe engine embodying one condition's (model, prompt, history)."""
    if condition == 'no-swap':
        engine = _make_engine(prefix, spec.system_prompt, spec.max_tokens)
        engine.messages = _fork_messages(prefix_messages)
        return engine
    if condition == 'swap-same':
        engine = _make_engine(prefix, '(pending checkpoint)', spec.max_tokens)
        return _apply_strict(engine, checkpoint)
    if condition == 'swap-diff':
        engine = _make_engine(suffix, '(pending checkpoint)', spec.max_tokens)
        return _apply_strict(engine, checkpoint)
    if condition == 'memory-off':
        # Charter intact, accumulated state severed: the IS floor.
        engine = _make_engine(suffix, spec.system_prompt, spec.max_tokens)
        engine.messages = []
        return engine
    if condition == 'scaffold-off':
        engine = _make_engine(suffix, '(pending checkpoint)', spec.max_tokens)
        return _apply_strict(engine, checkpoint,
                             system_prompt_override=spec.stripped_system_prompt)
    raise ValueError(f'unknown condition: {condition}')


async def _run_condition(
    condition: str,
    *,
    prefix: Backbone,
    suffix: Backbone,
    spec: TrajectorySpec,
    checkpoint: dict[str, Any],
    prefix_messages: list[Message],
) -> ConditionResult:
    responses: list[dict[str, Any]] = []
    usage_total = {'input_tokens': 0, 'output_tokens': 0}
    error = ''
    for probe in spec.probes:
        engine = _probe_engine_for_condition(
            condition, prefix=prefix, suffix=suffix, spec=spec,
            checkpoint=checkpoint, prefix_messages=prefix_messages,
        )
        try:
            reply, usage = await _submit_checked(engine, probe.text)
        except IpmsRunError as e:
            _diag('ipms', f'probe failed under condition {condition}', error=e)
            responses.append({
                'probe_id': probe.id, 'kind': probe.kind,
                'response': '', 'error': str(e),
            })
            error = error or str(e)
            continue
        usage_total['input_tokens'] += usage['input_tokens']
        usage_total['output_tokens'] += usage['output_tokens']
        responses.append({
            'probe_id': probe.id, 'kind': probe.kind,
            'response': reply, 'usage': usage,
        })
    return ConditionResult(
        condition=condition,
        prefix_model=prefix.name,
        suffix_model=suffix.name,
        probe_responses=responses,
        usage=usage_total,
        error=error,
    )


async def _run_pair_async(
    prefix: Backbone,
    suffix: Backbone,
    spec: TrajectorySpec,
    *,
    run_dir: Path,
    conditions: tuple[str, ...] = CONDITIONS,
    billing_state_dir: Path | None = None,
) -> PairResult:
    started = _now_iso()
    run_dir.mkdir(parents=True, exist_ok=True)

    # One prefix trajectory per (prefix backbone, spec); every condition
    # fans out from this single checkpoint so probes pair across conditions.
    engine_a = _make_engine(prefix, spec.system_prompt, spec.max_tokens)
    transcript: list[dict[str, Any]] = []
    for turn in spec.turns:
        reply, usage = await _submit_checked(engine_a, turn)
        transcript.append({'user': turn, 'assistant': reply, 'usage': usage})

    checkpoint = create_checkpoint_from_engine(
        engine_a, state_dir=run_dir,
        label=f'{spec.id}:{prefix.name}',
    )
    prefix_messages = _fork_messages(engine_a.messages)

    results: dict[str, ConditionResult] = {}
    for condition in conditions:
        results[condition] = await _run_condition(
            condition, prefix=prefix, suffix=suffix, spec=spec,
            checkpoint=checkpoint, prefix_messages=prefix_messages,
        )

    billing_mode = 'unknown'
    if billing_state_dir is not None:
        try:
            from charon.providers.model_registry import resolve_billing_mode
            billing_mode = resolve_billing_mode(Path(billing_state_dir))
        except Exception as e:
            _diag('ipms', 'billing mode resolution failed', error=e)

    pair = PairResult(
        spec_id=spec.id,
        prefix=prefix.name,
        suffix=suffix.name,
        checkpoint_id=checkpoint['id'],
        transcript=transcript,
        conditions=results,
        billing_mode=billing_mode,
        started_at=started,
        finished_at=_now_iso(),
    )
    _persist_pair(run_dir, pair)
    return pair


def run_pair(
    prefix: Backbone,
    suffix: Backbone,
    spec: TrajectorySpec,
    *,
    run_dir: Path | str,
    conditions: tuple[str, ...] = CONDITIONS,
    billing_state_dir: Path | str | None = None,
) -> PairResult:
    """Run all conditions for one directed backbone pair over one spec."""
    return asyncio.run(_run_pair_async(
        prefix, suffix, spec,
        run_dir=Path(run_dir),
        conditions=conditions,
        billing_state_dir=Path(billing_state_dir) if billing_state_dir else None,
    ))


def _persist_pair(run_dir: Path, pair: PairResult) -> Path:
    record = {
        'spec_id': pair.spec_id,
        'prefix': pair.prefix,
        'suffix': pair.suffix,
        'checkpoint_id': pair.checkpoint_id,
        'billing_mode': pair.billing_mode,
        'started_at': pair.started_at,
        'finished_at': pair.finished_at,
        'transcript': pair.transcript,
        'conditions': {
            name: {
                'condition': c.condition,
                'prefix_model': c.prefix_model,
                'suffix_model': c.suffix_model,
                'probe_responses': c.probe_responses,
                'usage': c.usage,
                'error': c.error,
            }
            for name, c in pair.conditions.items()
        },
    }
    safe_pair = f"{pair.prefix}__{pair.suffix}".replace('/', '-')
    path = run_dir / f'pair-{pair.spec_id}-{safe_pair}.json'
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    return path
