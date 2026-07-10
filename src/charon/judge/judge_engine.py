"""Judge engine — iterative optimization loops with pluggable scoring.

The core loop:
    implement → judge(score + feedback) → keep/rollback → repeat → converge

Judge types:
    - Quantitative: run a command, parse a number
    - Correctness: run tests, compute pass rate
    - Aesthetic: LLM scores against a rubric
    - Composite: weighted mix of multiple judges

The engine is tick-driven — each call to tick() advances by one step.
This keeps the daemon responsive and lets users interrupt anytime.

Usage:
    loop = JudgeLoop(config, state_dir, checkpoint_mgr)
    while not loop.converged:
        await loop.tick()  # one iteration
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ── Data types ──────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = 'jl') -> str:
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


# Default min_delta for stochastic (LLM) judges, set from measured score noise:
# AestheticJudge σ≈0.22 on a 1-10 scale (gpt-5.5; see results/judge_variance.json
# and scripts/experiments/measure_judge_variance.py). Use ~2σ so a single noise spike does
# not register as an improvement and get kept.
STOCHASTIC_JUDGE_MIN_DELTA = 0.5


@dataclass
class JudgeVerdict:
    """Result of a single judge evaluation."""
    score: float
    feedback: str
    raw_output: str = ''
    breakdown: dict[str, float] = field(default_factory=dict)  # for composite
    error: str | None = None


@dataclass
class Iteration:
    """Record of one optimization iteration."""
    iteration: int
    score: float | None = None
    feedback: str = ''
    change_summary: str = ''
    kept: bool = False
    status: str = 'pending'  # pending | running | scored | kept | discarded | crashed | constraint_failed | frozen_violation
    checkpoint_id: str | None = None
    implementer_contract_id: str | None = None
    judge_output: str = ''
    constraint_output: str = ''
    duration_seconds: float = 0.0
    timestamp: str = ''


@dataclass
class Convergence:
    """Why the loop stopped."""
    converged: bool = False
    reason: str = ''  # target_met | budget_exhausted | plateau | consecutive_failures | user_stopped | time_limit
    final_score: float | None = None
    best_score: float | None = None
    iterations_used: int = 0


@dataclass
class JudgeLoopConfig:
    """Full configuration for a judge loop."""
    id: str = ''
    goal: str = ''
    project: str = ''
    agent_id: str = ''

    # Judge
    judge_type: str = 'quantitative'  # quantitative | correctness | aesthetic | composite
    direction: str = 'maximize'       # maximize | minimize
    target_score: float | None = None

    # For quantitative / correctness judges
    eval_command: str = ''
    metric_name: str = 'score'
    parse_mode: str = 'last_float'    # last_float | json_field | pass_rate | custom_regex
    parse_field: str = ''             # JSON field name or regex pattern
    run_command: str = ''             # optional: command to run before eval (e.g. training)
    run_timeout: int = 600

    # For aesthetic judges
    rubric: str = ''

    # For composite judges
    sub_judges: list[dict] = field(default_factory=list)  # [{type, weight, ...config}]

    # Constraints (must pass before scoring)
    constraint_commands: list[str] = field(default_factory=list)  # e.g. ["pytest tests/ -x"]

    # Scope & budget
    scope: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    max_iterations: int = 20
    max_wall_minutes: int = 0           # 0 = no limit
    max_consecutive_failures: int = 5
    min_delta: float = 0.0              # absolute improvement floor (noise reject)
    min_delta_rel: float = 0.0          # relative improvement floor, fraction of |best|
    plateau_window: int = 5             # how many discards before declaring plateau

    # Program (instructions for the implementer shade)
    program: str = ''

    # State (mutated during execution)
    status: str = 'created'  # created | running | paused | completed | failed
    baseline: float | None = None
    best_score: float | None = None
    best_iteration: int = 0
    best_checkpoint: str | None = None
    current_iteration: int = 0
    consecutive_failures: int = 0
    iterations: list[Iteration] = field(default_factory=list)
    convergence: Convergence = field(default_factory=Convergence)
    started_at: str = ''
    completed_at: str = ''
    created_at: str = ''
    updated_at: str = ''


# ── Persistence ─────────────────────────────────────────────────────

def _loops_path(state_dir: Path) -> Path:
    return state_dir / 'judge_loops.json'


def _load_loops(state_dir: Path) -> list[dict]:
    p = _loops_path(state_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_loops(state_dir: Path, loops: list[dict]) -> None:
    p = _loops_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(loops, indent=2, default=str))


def save_loop(state_dir: Path, config: JudgeLoopConfig) -> None:
    """Persist a judge loop config to disk."""
    loops = _load_loops(state_dir)
    data = _config_to_dict(config)
    # Update existing or append
    for i, loop in enumerate(loops):
        if loop.get('id') == config.id:
            loops[i] = data
            _save_loops(state_dir, loops)
            return
    loops.append(data)
    _save_loops(state_dir, loops)


def load_loop(state_dir: Path, loop_id: str) -> JudgeLoopConfig | None:
    """Load a judge loop config by ID."""
    for loop in _load_loops(state_dir):
        if loop.get('id') == loop_id:
            return _dict_to_config(loop)
    return None


def list_loops(state_dir: Path) -> list[dict]:
    """List all judge loops (summary dicts)."""
    return _load_loops(state_dir)


def _config_to_dict(config: JudgeLoopConfig) -> dict:
    """Serialize config to a JSON-safe dict."""
    d = {}
    for k, v in config.__dict__.items():
        if k == 'iterations':
            d[k] = [it.__dict__ for it in v]
        elif k == 'convergence':
            d[k] = v.__dict__
        else:
            d[k] = v
    return d


def _dict_to_config(d: dict) -> JudgeLoopConfig:
    """Deserialize a dict to a JudgeLoopConfig."""
    config = JudgeLoopConfig()
    for k, v in d.items():
        if k == 'iterations':
            config.iterations = [Iteration(**it) for it in (v or [])]
        elif k == 'convergence':
            config.convergence = Convergence(**(v or {}))
        elif hasattr(config, k):
            setattr(config, k, v)
    return config


# ── Event log ───────────────────────────────────────────────────────

def _events_path(state_dir: Path) -> Path:
    return state_dir / 'judge_loop_events.jsonl'


def _append_event(state_dir: Path, loop_id: str, event_type: str, payload: dict | None = None) -> None:
    p = _events_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        'ts': _now(),
        'loop_id': loop_id,
        'event_type': event_type,
        'payload': payload or {},
    }
    with p.open('a') as f:
        f.write(json.dumps(entry, default=str) + '\n')


# ── Judge Adapters ──────────────────────────────────────────────────

class JudgeAdapter(ABC):
    """Base class for judge implementations."""

    @abstractmethod
    def evaluate(self, config: JudgeLoopConfig, working_dir: Path) -> JudgeVerdict:
        """Score the current state. Returns a verdict with score + feedback."""
        ...


class QuantitativeJudge(JudgeAdapter):
    """Run a command, parse a number from the output."""

    def evaluate(self, config: JudgeLoopConfig, working_dir: Path) -> JudgeVerdict:
        # Run the eval command
        if config.run_command:
            run_result = _run_cmd(config.run_command, working_dir, timeout=config.run_timeout)
            if run_result.returncode != 0:
                return JudgeVerdict(
                    score=float('nan'),
                    feedback=f'Run command failed (exit {run_result.returncode}): {run_result.stderr[:500]}',
                    raw_output=run_result.stdout + run_result.stderr,
                    error='run_failed',
                )

        if not config.eval_command:
            return JudgeVerdict(score=float('nan'), feedback='No eval_command configured', error='no_eval')

        result = _run_cmd(config.eval_command, working_dir, timeout=120)
        raw = result.stdout + result.stderr

        if result.returncode != 0:
            return JudgeVerdict(
                score=float('nan'),
                feedback=f'Eval command failed (exit {result.returncode}): {result.stderr[:500]}',
                raw_output=raw,
                error='eval_failed',
            )

        score = _parse_score(raw, config.parse_mode, config.parse_field)
        if score is None or math.isnan(score):
            return JudgeVerdict(
                score=float('nan'),
                feedback=f'Could not parse score from output: {raw[:300]}',
                raw_output=raw,
                error='parse_failed',
            )

        # Generate feedback from the output
        feedback = _generate_quantitative_feedback(config, score, raw)
        return JudgeVerdict(score=score, feedback=feedback, raw_output=raw)


class CorrectnessJudge(JudgeAdapter):
    """Run a test suite, compute pass rate as score (0.0 - 1.0)."""

    def evaluate(self, config: JudgeLoopConfig, working_dir: Path) -> JudgeVerdict:
        cmd = config.eval_command or 'pytest --tb=short -q'
        result = _run_cmd(cmd, working_dir, timeout=config.run_timeout or 300)
        raw = result.stdout + result.stderr

        # Parse pytest-style output: "X passed, Y failed"
        passed = 0
        failed = 0
        errors = 0

        m = re.search(r'(\d+)\s+passed', raw)
        if m:
            passed = int(m.group(1))
        m = re.search(r'(\d+)\s+failed', raw)
        if m:
            failed = int(m.group(1))
        m = re.search(r'(\d+)\s+error', raw)
        if m:
            errors = int(m.group(1))

        total = passed + failed + errors
        if total == 0:
            return JudgeVerdict(
                score=0.0,
                feedback='No tests found or test runner failed',
                raw_output=raw,
                error='no_tests',
            )

        score = passed / total
        feedback_parts = [f'{passed}/{total} tests passed ({score:.1%})']
        if failed:
            # Extract failure names
            failure_lines = re.findall(r'FAILED\s+(.+?)(?:\s+-|$)', raw)
            if failure_lines:
                feedback_parts.append(f'Failing: {", ".join(failure_lines[:5])}')
        if errors:
            feedback_parts.append(f'{errors} errors')

        return JudgeVerdict(
            score=score,
            feedback='. '.join(feedback_parts),
            raw_output=raw,
        )


class AestheticJudge(JudgeAdapter):
    """LLM-as-judge — scores against a freeform rubric.

    Spawns a separate LLM call (not a shade) to evaluate. The LLM
    returns a JSON object with score and feedback.
    """

    def __init__(self, provider=None, model=None):
        self._provider = provider
        self._model = model

    def evaluate(self, config: JudgeLoopConfig, working_dir: Path) -> JudgeVerdict:
        """Evaluate using LLM-as-judge.

        Reads the files in scope and asks the LLM to score them
        against the rubric. Returns structured verdict.
        """
        # Gather files in scope for the LLM to review
        file_contents = _gather_scope_contents(config.scope, working_dir)
        if not file_contents:
            return JudgeVerdict(
                score=0.0,
                feedback='No files found in scope to evaluate',
                error='no_files',
            )

        rubric = config.rubric or 'Rate the overall quality on a scale of 1-10.'
        goal = config.goal or 'Improve the code'

        prompt = _build_aesthetic_judge_prompt(
            rubric=rubric,
            goal=goal,
            file_contents=file_contents,
            history=config.iterations,
        )

        # Use the provider to get a score
        try:
            verdict = _call_llm_judge(prompt, self._provider, self._model)
            return verdict
        except Exception as e:
            return JudgeVerdict(
                score=0.0,
                feedback=f'LLM judge error: {e}',
                error='llm_failed',
            )


class CompositeJudge(JudgeAdapter):
    """Weighted combination of multiple judges."""

    def __init__(self, sub_judges: list[tuple[JudgeAdapter, float]]):
        self.sub_judges = sub_judges  # [(adapter, weight), ...]

    def evaluate(self, config: JudgeLoopConfig, working_dir: Path) -> JudgeVerdict:
        total_weight = sum(w for _, w in self.sub_judges)
        if total_weight == 0:
            return JudgeVerdict(score=0.0, feedback='No sub-judges configured', error='no_judges')

        weighted_sum = 0.0
        feedbacks = []
        breakdown = {}
        errors = []

        for adapter, weight in self.sub_judges:
            verdict = adapter.evaluate(config, working_dir)
            name = type(adapter).__name__

            if verdict.error:
                errors.append(f'{name}: {verdict.error}')
                continue

            if math.isnan(verdict.score):
                errors.append(f'{name}: NaN score')
                continue

            normalized_weight = weight / total_weight
            weighted_sum += verdict.score * normalized_weight
            breakdown[name] = verdict.score
            feedbacks.append(f'{name} ({weight:.1f}w): {verdict.score:.2f} — {verdict.feedback}')

        feedback = '\n'.join(feedbacks)
        if errors:
            feedback += '\nErrors: ' + '; '.join(errors)

        return JudgeVerdict(
            score=weighted_sum,
            feedback=feedback,
            breakdown=breakdown,
        )


# ── Score parsing ───────────────────────────────────────────────────

def _parse_score(output: str, mode: str, parse_field: str = '') -> float | None:
    """Extract a numeric score from command output."""
    if mode == 'json_field':
        return _parse_json_field(output, parse_field)
    elif mode == 'pass_rate':
        return _parse_pass_rate(output)
    elif mode == 'custom_regex':
        return _parse_regex(output, parse_field)
    else:  # last_float
        return _parse_last_float(output)


def _parse_last_float(output: str) -> float | None:
    """Find the last floating point number in the output."""
    matches = re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', output)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _parse_json_field(output: str, field: str) -> float | None:
    """Parse a JSON object from output and extract a field."""
    # Try to find JSON in the output
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                data = json.loads(line)
                val = data.get(field)
                if val is not None:
                    return float(val)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    return None


def _parse_pass_rate(output: str) -> float | None:
    """Parse pytest-style output for pass rate."""
    passed = 0
    total = 0
    m = re.search(r'(\d+)\s+passed', output)
    if m:
        passed = int(m.group(1))
    m = re.search(r'(\d+)\s+failed', output)
    if m:
        total += int(m.group(1))
    m = re.search(r'(\d+)\s+error', output)
    if m:
        total += int(m.group(1))
    total += passed
    if total == 0:
        return None
    return passed / total


def _parse_regex(output: str, pattern: str) -> float | None:
    """Extract score using a custom regex with a capturing group."""
    m = re.search(pattern, output)
    if m and m.groups():
        try:
            return float(m.group(1))
        except (ValueError, IndexError):
            return None
    return None


# ── Helpers ─────────────────────────────────────────────────────────

def _run_cmd(cmd: str, working_dir: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(
        ['bash', '-c', cmd],
        cwd=str(working_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _generate_quantitative_feedback(config: JudgeLoopConfig, score: float, raw: str) -> str:
    """Generate human-readable feedback for a quantitative score."""
    parts = [f'{config.metric_name}: {score}']

    if config.best_score is not None:
        if config.direction == 'maximize':
            delta = score - config.best_score
        else:
            delta = config.best_score - score
        if delta > 0:
            parts.append(f'improvement: +{abs(delta):.4f}')
        elif delta < 0:
            parts.append(f'regression: -{abs(delta):.4f}')
        else:
            parts.append('no change')

    if config.target_score is not None:
        if config.direction == 'maximize':
            gap = config.target_score - score
            if gap <= 0:
                parts.append('TARGET MET')
            else:
                parts.append(f'gap to target: {gap:.4f}')
        else:
            gap = score - config.target_score
            if gap <= 0:
                parts.append('TARGET MET')
            else:
                parts.append(f'gap to target: {gap:.4f}')

    return '. '.join(parts)


def _gather_scope_contents(scope: list[str], working_dir: Path, max_chars: int = 30_000) -> str:
    """Read files in scope for LLM judge evaluation."""
    parts = []
    total = 0
    for entry in scope:
        full = working_dir / entry
        if full.is_file():
            try:
                content = full.read_text(encoding='utf-8', errors='replace')
                if total + len(content) > max_chars:
                    content = content[:max_chars - total] + '\n[...truncated]'
                parts.append(f'### {entry}\n```\n{content}\n```')
                total += len(content)
                if total >= max_chars:
                    break
            except Exception:
                continue
        elif full.is_dir():
            for f in sorted(full.rglob('*')):
                if f.is_file() and f.suffix in ('.py', '.js', '.ts', '.md', '.yaml', '.toml', '.json', '.rs', '.go'):
                    try:
                        rel = f.relative_to(working_dir)
                        content = f.read_text(encoding='utf-8', errors='replace')
                        if total + len(content) > max_chars:
                            content = content[:max_chars - total] + '\n[...truncated]'
                        parts.append(f'### {rel}\n```\n{content}\n```')
                        total += len(content)
                        if total >= max_chars:
                            break
                    except Exception:
                        continue
    return '\n\n'.join(parts)


def _build_aesthetic_judge_prompt(
    rubric: str,
    goal: str,
    file_contents: str,
    history: list[Iteration],
) -> str:
    """Build the prompt for an LLM aesthetic judge."""
    parts = [
        'You are a code quality judge. Score the following files against the rubric below.',
        '',
        '## Goal',
        goal,
        '',
        '## Rubric',
        rubric,
        '',
        '## Scoring',
        'Return a JSON object on a single line:',
        '{"score": <float 0-10>, "feedback": "<specific actionable critique>"}',
        '',
        'Be precise. Reference specific lines, functions, or sections.',
        'The feedback will be given to an implementer to guide the next iteration.',
    ]

    if history:
        kept = [it for it in history if it.kept]
        discarded = [it for it in history if not it.kept and it.status not in ('pending', 'running')]
        if kept or discarded:
            parts.append('')
            parts.append('## History')
            for it in history[-5:]:
                status = 'KEPT' if it.kept else it.status.upper()
                parts.append(f'- iter {it.iteration}: {it.score} ({status}) — {it.change_summary[:100]}')

    parts.append('')
    parts.append('## Files to Evaluate')
    parts.append(file_contents)

    return '\n'.join(parts)


def _call_llm_judge(prompt: str, provider=None, model=None) -> JudgeVerdict:
    """Call the LLM to get a judge verdict.

    Uses the provider if available, otherwise falls back to a subprocess
    call to the Charon daemon's eval endpoint.
    """
    if provider and model:
        # Synchronous LLM call via provider
        import asyncio
        from charon.providers import Message

        async def _call():
            messages = [Message(role='user', content=prompt, timestamp=time.time())]
            full_text = []
            async for delta in provider.stream(
                messages=messages,
                model=model,
                system_prompt='You are a precise judge. Return only the JSON score object.',
                max_tokens=500,
            ):
                if delta.type == 'text':
                    full_text.append(delta.text)
            return ''.join(full_text)

        try:
            # asyncio.run() closes the loop it creates, so a cached
            # get_event_loop() handle goes stale after the first call (every
            # subsequent aesthetic scoring would fail with "no current event
            # loop"). Detect a *running* loop instead: if one is running we're
            # in async code and must offload to a thread; otherwise a fresh
            # asyncio.run() is correct each time.
            try:
                asyncio.get_running_loop()
                in_running_loop = True
            except RuntimeError:
                in_running_loop = False

            if in_running_loop:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    response = pool.submit(asyncio.run, _call()).result(timeout=60)
            else:
                response = asyncio.run(_call())
        except Exception as e:
            return JudgeVerdict(score=0.0, feedback=f'LLM call failed: {e}', error='llm_call_failed')
    else:
        return JudgeVerdict(score=0.0, feedback='No LLM provider configured for aesthetic judge', error='no_provider')

    # Parse the JSON response
    return _parse_llm_verdict(response)


def _parse_llm_verdict(response: str) -> JudgeVerdict:
    """Parse LLM response into a JudgeVerdict."""
    # Try to find JSON in the response
    for line in response.splitlines():
        line = line.strip()
        if line.startswith('{') and 'score' in line:
            try:
                data = json.loads(line)
                return JudgeVerdict(
                    score=float(data.get('score', 0)),
                    feedback=str(data.get('feedback', '')),
                    raw_output=response,
                )
            except (json.JSONDecodeError, ValueError):
                continue

    # Fallback: try to find JSON block anywhere
    m = re.search(r'\{[^}]*"score"\s*:\s*([\d.]+)[^}]*"feedback"\s*:\s*"([^"]*)"[^}]*\}', response)
    if m:
        return JudgeVerdict(
            score=float(m.group(1)),
            feedback=m.group(2),
            raw_output=response,
        )

    return JudgeVerdict(
        score=0.0,
        feedback=f'Could not parse judge response: {response[:200]}',
        raw_output=response,
        error='parse_failed',
    )


# ── Factory ─────────────────────────────────────────────────────────

def create_judge(config: JudgeLoopConfig, provider=None, model=None) -> JudgeAdapter:
    """Create the appropriate judge adapter from config."""
    jtype = config.judge_type

    if jtype == 'quantitative':
        return QuantitativeJudge()
    elif jtype == 'correctness':
        return CorrectnessJudge()
    elif jtype == 'aesthetic':
        return AestheticJudge(provider=provider, model=model)
    elif jtype == 'composite':
        subs = []
        for sub in config.sub_judges:
            sub_config = JudgeLoopConfig(**{k: v for k, v in sub.items() if k != 'weight' and hasattr(JudgeLoopConfig, k)})
            sub_config.eval_command = sub.get('eval_command', config.eval_command)
            sub_config.rubric = sub.get('rubric', config.rubric)
            sub_config.scope = sub.get('scope', config.scope)
            sub_config.run_command = sub.get('run_command', config.run_command)
            sub_config.run_timeout = sub.get('run_timeout', config.run_timeout)
            sub_adapter = create_judge(sub_config, provider=provider, model=model)
            weight = float(sub.get('weight', 1.0))
            subs.append((sub_adapter, weight))
        return CompositeJudge(subs)
    else:
        # Default to quantitative
        return QuantitativeJudge()


# ── Convergence detection ───────────────────────────────────────────

def check_convergence(config: JudgeLoopConfig) -> Convergence | None:
    """Check if the loop should stop. Returns Convergence if done, None if should continue."""

    # Budget exhausted
    if config.current_iteration >= config.max_iterations:
        return Convergence(
            converged=False,
            reason='budget_exhausted',
            final_score=config.best_score,
            best_score=config.best_score,
            iterations_used=config.current_iteration,
        )

    # Consecutive failures
    if config.consecutive_failures >= config.max_consecutive_failures:
        return Convergence(
            converged=False,
            reason='consecutive_failures',
            final_score=config.best_score,
            best_score=config.best_score,
            iterations_used=config.current_iteration,
        )

    # Wall time
    if config.max_wall_minutes > 0 and config.started_at:
        try:
            started = datetime.fromisoformat(config.started_at)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 60
            if elapsed >= config.max_wall_minutes:
                return Convergence(
                    converged=False,
                    reason='time_limit',
                    final_score=config.best_score,
                    best_score=config.best_score,
                    iterations_used=config.current_iteration,
                )
        except Exception:
            pass

    # Target met
    if config.target_score is not None and config.best_score is not None:
        if config.direction == 'maximize' and config.best_score >= config.target_score:
            return Convergence(
                converged=True,
                reason='target_met',
                final_score=config.best_score,
                best_score=config.best_score,
                iterations_used=config.current_iteration,
            )
        elif config.direction == 'minimize' and config.best_score <= config.target_score:
            return Convergence(
                converged=True,
                reason='target_met',
                final_score=config.best_score,
                best_score=config.best_score,
                iterations_used=config.current_iteration,
            )

    # Plateau detection
    if config.plateau_window > 0 and len(config.iterations) >= config.plateau_window:
        recent = config.iterations[-config.plateau_window:]
        if all(not it.kept for it in recent) and all(it.status in ('discarded', 'scored') for it in recent):
            return Convergence(
                converged=False,
                reason='plateau',
                final_score=config.best_score,
                best_score=config.best_score,
                iterations_used=config.current_iteration,
            )

    return None


# ── Loop controller ─────────────────────────────────────────────────

def is_improvement(score: float, best: float, direction: str, min_delta: float = 0.0,
                   min_delta_rel: float = 0.0) -> bool:
    """Check if a new score is a real improvement over the current best.

    The required gain must clear the larger of two noise floors:
      - min_delta:     an ABSOLUTE floor (right for judges whose noise is
                       absolute, e.g. an aesthetic judge with σ≈0.22 on 0–10).
      - min_delta_rel: a RELATIVE floor, as a fraction of |best| (right for
                       metrics that span orders of magnitude, e.g. wall-clock
                       that starts at 1.8s and optimizes down to milliseconds —
                       a fixed absolute delta would go blind once the metric
                       shrinks below it).
    """
    if math.isnan(score):
        return False
    delta = max(min_delta, min_delta_rel * abs(best))
    if direction == 'maximize':
        return score > best + delta
    else:
        return score < best - delta


def build_iteration_prompt(config: JudgeLoopConfig) -> str:
    """Build the instruction for the implementer shade.

    Includes the program, current state, score history, and
    guidance on what to try next.
    """
    parts = [f'# Optimization Iteration {config.current_iteration + 1}']

    if config.program:
        parts.append('')
        parts.append('## Program')
        parts.append(config.program)

    parts.append('')
    parts.append('## Current State')
    parts.append(f'- Metric: {config.metric_name} ({config.direction})')
    if config.baseline is not None:
        parts.append(f'- Baseline: {config.baseline}')
    if config.best_score is not None:
        parts.append(f'- Current best: {config.best_score} (iteration {config.best_iteration})')
    if config.target_score is not None:
        parts.append(f'- Target: {config.target_score}')
    parts.append(f'- Iterations used: {config.current_iteration} / {config.max_iterations}')

    # Recent history
    if config.iterations:
        parts.append('')
        parts.append('## Recent History')
        recent = config.iterations[-8:]
        for it in recent:
            score_str = f'{it.score}' if it.score is not None else '?'
            status = 'KEPT ✓' if it.kept else it.status.upper()
            parts.append(f'- iter {it.iteration}: {score_str} ({status}) — {it.change_summary[:120]}')
            if it.feedback and not it.kept:
                parts.append(f'  Feedback: {it.feedback[:200]}')

    parts.append('')
    parts.append('## Your Task')
    parts.append('Propose and implement ONE focused change.')
    parts.append(f'Files in scope: {", ".join(config.scope) if config.scope else "any"}')
    if config.frozen:
        parts.append(f'Do NOT modify: {", ".join(config.frozen)}')
    parts.append('')
    parts.append('Do NOT repeat changes that were already tried and discarded.')
    parts.append('Keep the change minimal and focused. Explain your reasoning.')

    # Add last feedback for guidance
    last_scored = [it for it in config.iterations if it.feedback and it.status != 'pending']
    if last_scored:
        last = last_scored[-1]
        parts.append('')
        parts.append('## Last Judge Feedback')
        parts.append(last.feedback[:500])

    return '\n'.join(parts)


def create_loop(
    state_dir: Path,
    *,
    goal: str,
    project: str,
    agent_id: str,
    judge_type: str = 'quantitative',
    direction: str = 'maximize',
    target_score: float | None = None,
    eval_command: str = '',
    metric_name: str = 'score',
    parse_mode: str = 'last_float',
    parse_field: str = '',
    run_command: str = '',
    run_timeout: int = 600,
    rubric: str = '',
    sub_judges: list[dict] | None = None,
    constraint_commands: list[str] | None = None,
    scope: list[str] | None = None,
    frozen: list[str] | None = None,
    max_iterations: int = 20,
    max_wall_minutes: int = 0,
    max_consecutive_failures: int = 5,
    min_delta: float | None = None,
    min_delta_rel: float = 0.0,
    plateau_window: int = 5,
    program: str = '',
) -> JudgeLoopConfig:
    """Create and persist a new judge loop."""
    # Stochastic (LLM) judges have a measurable score-noise floor; if min_delta
    # is below it, the loop hill-climbs noise. Measured AestheticJudge noise is
    # σ≈0.22 (gpt-5.5, results/judge_variance.json); default min_delta to ≈2σ
    # for aesthetic/composite so a single noise spike isn't "kept" as progress.
    # Deterministic judges (quantitative/correctness) keep min_delta=0.
    if min_delta is None:
        min_delta = STOCHASTIC_JUDGE_MIN_DELTA if judge_type in ('aesthetic', 'composite') else 0.0
    config = JudgeLoopConfig(
        id=_new_id('jl'),
        goal=goal,
        project=project,
        agent_id=agent_id,
        judge_type=judge_type,
        direction=direction,
        target_score=target_score,
        eval_command=eval_command,
        metric_name=metric_name,
        parse_mode=parse_mode,
        parse_field=parse_field,
        run_command=run_command,
        run_timeout=run_timeout,
        rubric=rubric,
        sub_judges=sub_judges or [],
        constraint_commands=constraint_commands or [],
        scope=scope or [],
        frozen=frozen or [],
        max_iterations=max_iterations,
        max_wall_minutes=max_wall_minutes,
        max_consecutive_failures=max_consecutive_failures,
        min_delta=min_delta,
        min_delta_rel=min_delta_rel,
        plateau_window=plateau_window,
        program=program,
        status='created',
        created_at=_now(),
        updated_at=_now(),
    )
    save_loop(state_dir, config)
    _append_event(state_dir, config.id, 'loop_created', {
        'goal': goal, 'judge_type': judge_type, 'max_iterations': max_iterations,
    })
    return config


def run_baseline(
    config: JudgeLoopConfig,
    judge: JudgeAdapter,
    working_dir: Path,
    checkpoint_mgr=None,
) -> JudgeLoopConfig:
    """Run the baseline measurement (iteration 0).

    Must be called before starting the iteration loop.
    """
    config.status = 'running'
    config.started_at = _now()

    # Checkpoint the starting state
    if checkpoint_mgr:
        cp_id = checkpoint_mgr.snapshot('baseline')
    else:
        cp_id = None

    # Run constraint checks first
    for cmd in config.constraint_commands:
        result = _run_cmd(cmd, working_dir, timeout=120)
        if result.returncode != 0:
            config.status = 'failed'
            config.updated_at = _now()
            return config

    # Evaluate baseline
    verdict = judge.evaluate(config, working_dir)

    if verdict.error and math.isnan(verdict.score):
        config.status = 'failed'
        config.updated_at = _now()
        return config

    config.baseline = verdict.score
    config.best_score = verdict.score
    config.best_iteration = 0
    config.best_checkpoint = cp_id

    baseline_iter = Iteration(
        iteration=0,
        score=verdict.score,
        feedback=verdict.feedback,
        change_summary='baseline measurement',
        kept=True,
        status='kept',
        checkpoint_id=cp_id,
        judge_output=verdict.raw_output[:500],
        timestamp=_now(),
    )
    config.iterations.append(baseline_iter)
    config.updated_at = _now()

    return config


def run_iteration(
    config: JudgeLoopConfig,
    judge: JudgeAdapter,
    working_dir: Path,
    change_summary: str,
    checkpoint_mgr=None,
    implementer_contract_id: str = '',
) -> tuple[JudgeLoopConfig, Iteration]:
    """Run one iteration: checkpoint → constraints → judge → keep/rollback.

    The actual implementation (code changes) must happen BEFORE calling this.
    This function handles: checkpoint, constraint check, scoring, keep/rollback.

    Returns (updated_config, iteration_record).
    """
    start_time = time.time()
    config.current_iteration += 1
    iteration_num = config.current_iteration

    # Checkpoint current state (post-implementation)
    cp_id = None
    if checkpoint_mgr:
        cp_id = checkpoint_mgr.snapshot(f'iter-{iteration_num}')

    iteration = Iteration(
        iteration=iteration_num,
        change_summary=change_summary,
        status='running',
        checkpoint_id=cp_id,
        implementer_contract_id=implementer_contract_id,
        timestamp=_now(),
    )

    # Frozen-path gate: a hard anti-gaming check. If this iteration touched any
    # frozen path (vs the best-known checkpoint) — by any means, including a
    # shell command that the tool-layer scope check can't see — reject and roll
    # back before scoring, so the optimizer cannot "win" by editing files it was
    # told not to touch.
    if config.frozen and checkpoint_mgr and config.best_checkpoint:
        touched = checkpoint_mgr.changed_paths_under(config.best_checkpoint, config.frozen)
        if touched:
            iteration.status = 'frozen_violation'
            iteration.feedback = f'Frozen-path violation: modified {", ".join(touched[:5])}'
            iteration.kept = False
            iteration.duration_seconds = time.time() - start_time
            config.consecutive_failures += 1
            config.iterations.append(iteration)
            config.updated_at = _now()
            checkpoint_mgr.rollback(config.best_checkpoint)
            return config, iteration

    # Run constraint checks
    for cmd in config.constraint_commands:
        result = _run_cmd(cmd, working_dir, timeout=120)
        if result.returncode != 0:
            iteration.status = 'constraint_failed'
            iteration.constraint_output = (result.stdout + result.stderr)[:500]
            iteration.feedback = f'Constraint failed: {cmd}'
            iteration.kept = False
            iteration.duration_seconds = time.time() - start_time
            config.consecutive_failures += 1
            config.iterations.append(iteration)
            config.updated_at = _now()

            # Rollback
            if checkpoint_mgr and config.best_checkpoint:
                checkpoint_mgr.rollback(config.best_checkpoint)

            return config, iteration

    # Score with judge
    verdict = judge.evaluate(config, working_dir)

    iteration.score = verdict.score
    iteration.feedback = verdict.feedback
    iteration.judge_output = verdict.raw_output[:500]

    if verdict.error or math.isnan(verdict.score):
        iteration.status = 'crashed'
        iteration.kept = False
        config.consecutive_failures += 1

        # Rollback
        if checkpoint_mgr and config.best_checkpoint:
            checkpoint_mgr.rollback(config.best_checkpoint)
    elif is_improvement(verdict.score, config.best_score or 0.0, config.direction,
                        config.min_delta, config.min_delta_rel):
        # Improvement — keep it
        iteration.status = 'kept'
        iteration.kept = True
        config.best_score = verdict.score
        config.best_iteration = iteration_num
        config.best_checkpoint = cp_id
        config.consecutive_failures = 0
    else:
        # No improvement — rollback
        iteration.status = 'discarded'
        iteration.kept = False
        config.consecutive_failures += 1

        if checkpoint_mgr and config.best_checkpoint:
            checkpoint_mgr.rollback(config.best_checkpoint)

    iteration.duration_seconds = time.time() - start_time
    config.iterations.append(iteration)
    config.updated_at = _now()

    return config, iteration


def format_status(config: JudgeLoopConfig) -> str:
    """Format a human-readable status summary of a judge loop."""
    lines = [f'**Judge Loop: {config.goal}** (`{config.id}`)']
    lines.append('')

    if config.status == 'created':
        lines.append('Status: created (not yet started)')
        return '\n'.join(lines)

    lines.append('| | Value |')
    lines.append('|---|---|')
    lines.append(f'| Iterations | {config.current_iteration} / {config.max_iterations} |')
    if config.baseline is not None:
        lines.append(f'| Baseline | {config.baseline} |')
    if config.best_score is not None:
        lines.append(f'| Best | {config.best_score} (iter {config.best_iteration}) |')
    if config.baseline is not None and config.best_score is not None and config.baseline != 0:
        if config.direction == 'maximize':
            pct = ((config.best_score - config.baseline) / abs(config.baseline)) * 100
        else:
            pct = ((config.baseline - config.best_score) / abs(config.baseline)) * 100
        lines.append(f'| Improvement | {pct:+.1f}% |')
    if config.target_score is not None:
        lines.append(f'| Target | {config.target_score} |')

    kept_count = sum(1 for it in config.iterations if it.kept and it.iteration > 0)
    total = len([it for it in config.iterations if it.iteration > 0])
    lines.append(f'| Kept | {kept_count} / {total} |')
    lines.append(f'| Status | {config.status} |')

    # Last N iterations
    recent = [it for it in config.iterations if it.iteration > 0][-5:]
    if recent:
        lines.append('')
        lines.append('**Recent iterations:**')
        lines.append('| # | Change | Score | Status |')
        lines.append('|---|--------|-------|--------|')
        for it in recent:
            score_str = f'{it.score}' if it.score is not None else '?'
            status = '**kept** ✓' if it.kept else it.status
            summary = it.change_summary[:60] if it.change_summary else '—'
            lines.append(f'| {it.iteration} | {summary} | {score_str} | {status} |')

    if config.convergence.reason:
        lines.append('')
        lines.append(f'**Stopped:** {config.convergence.reason}')

    return '\n'.join(lines)


__all__ = [
    'JudgeLoopConfig', 'JudgeVerdict', 'Iteration', 'Convergence',
    'JudgeAdapter', 'QuantitativeJudge', 'CorrectnessJudge', 'AestheticJudge', 'CompositeJudge',
    'create_judge', 'check_convergence', 'is_improvement',
    'build_iteration_prompt', 'format_status',
    'create_loop', 'run_baseline', 'run_iteration',
    'save_loop', 'load_loop', 'list_loops',
]
