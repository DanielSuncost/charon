"""Reasoning-effort levels: one ladder, what each model accepts, and clamping.

Three places used to disagree about effort. The CLI and the engine accepted
off|minimal|low|medium|high|xhigh and aliased ``max`` to ``high``; the Codex
transport then folded ``xhigh`` into ``high``. Two sources say what is real:

* The **wire**. A live request with an unsupported value made the Codex
  Responses endpoint answer (2026-09-15):
  ``Invalid value: 'ultra'. Supported values are: 'none', 'minimal', 'low',
  'medium', 'high', 'xhigh', and 'max'.``  That enum is what ``reasoning.effort``
  accepts. ``none`` is Charon's ``off`` (the block is omitted).
* The **Codex CLI's model list** (``~/.codex/models_cache.json`` as fetched by
  codex-cli 0.154.0 the same day), which is what the CLI *offers* per model:

      gpt-6-astra    low medium high xhigh max ultra   default medium
      gpt-5.6-sol    low medium high xhigh max ultra   default low
      gpt-5.6-terra  low medium high xhigh max ultra   default medium
      gpt-5.6-luna   low medium high xhigh max         default medium
      gpt-reserve    low medium high xhigh max         default medium
      gpt-5.5        low medium high xhigh             default medium

  ``ultra`` there is a CLI mode ("maximum reasoning with automatic task
  delegation" — ``max`` plus the CLI's own multi-agent fan-out), not a wire
  value; Charon does its own delegation, so ``ultra`` is accepted as a level
  and sent as ``max``.

The set is enforced **per model**, not just by the enum. Live, 2026-09-15:

* astra + ``minimal`` → ``Unsupported value: 'minimal' is not supported with
  the 'gpt-6-astra' model. Supported values are: 'low', 'medium', 'high',
  'xhigh', and 'max'.``
* gpt-5.5 + ``max`` → ``Unsupported value: 'max' is not supported with the
  'gpt-5.5' model. Supported values are: 'none', 'low', 'medium', 'high', and
  'xhigh'.``

So the CLI's advertised ladder *is* the wire ladder, minus ``ultra``; a row
below is exactly that, ``minimal`` folds to the floor everywhere, and every
astra level plus ``ultra`` → ``max`` was accepted live
(tests/test_codex_effort_live.py). The table is static on purpose — Charon
does not read the Codex CLI's files at runtime. A model missing from it is
learned at runtime instead (``learn_efforts``), from the same two sources:
the provider's model listing (``charon.providers.model_catalog``) and a 400
naming the model's supported values (``parse_supported_efforts``). Learned
ladders win over the table; a model known to neither gets
``CODEX_DEFAULT_EFFORTS``.
"""
from __future__ import annotations

import re

EFFORT_LADDER: tuple[str, ...] = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
THINKING_LEVELS: tuple[str, ...] = ('off',) + EFFORT_LADDER

_ALIASES = {'': 'off', 'none': 'off', '0': 'off', 'min': 'minimal', 'med': 'medium'}

# Wire-level ladder per model: the CLI's advertised ladder with 'ultra' (a CLI
# mode) left out. 'minimal' is not a Codex model level; it folds to the floor.
CODEX_EFFORTS: dict[str, tuple[str, ...]] = {
    'gpt-6-astra': ('low', 'medium', 'high', 'xhigh', 'max'),              # all five accepted live; minimal rejected
    'gpt-5.6-sol': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.6-terra': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-reserve': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.5': ('low', 'medium', 'high', 'xhigh'),                         # 'max' rejected live
}
# Models the table does not know keep exactly what every Codex model was sent
# before this existed: low..high, with 'minimal' folded to 'low'.
CODEX_DEFAULT_EFFORTS: tuple[str, ...] = ('low', 'medium', 'high')
# DeepSeek's reasoning_effort ladder (charon.providers.deepseek has the source);
# its listing's effort.supported_levels, when fetched, is learned over this.
DEEPSEEK_EFFORTS: tuple[str, ...] = ('low', 'high', 'max')


# Ladders learned at runtime (provider listing or a rejection), by exact slug.
_LEARNED_EFFORTS: dict[str, tuple[str, ...]] = {}

_PER_MODEL_REJECTION = re.compile(r"not supported with the '([^']+)' model", re.IGNORECASE)
_SUPPORTED_VALUES = re.compile(r'Supported values are:(.*)', re.IGNORECASE | re.DOTALL)


def learn_efforts(model_id: str, levels) -> tuple[str, ...] | None:
    """Record the wire ladder a model accepts; returns it, or None if empty."""
    mid = str(model_id or '').strip().lower()
    rank = {name: i for i, name in enumerate(EFFORT_LADDER)}
    ladder = tuple(sorted(
        {str(lvl).strip().lower() for lvl in levels or ()} & (set(EFFORT_LADDER) - {'ultra'}),
        key=rank.__getitem__,
    ))
    if not mid or not ladder:
        return None
    _LEARNED_EFFORTS[mid] = ladder
    return ladder


def parse_supported_efforts(error_text: str, model_id: str) -> tuple[str, ...] | None:
    """The ladder named by a per-model effort rejection for `model_id`, if any.

    Only the per-model form ("... is not supported with the 'X' model.
    Supported values are: ...") is trusted; the generic enum error lists every
    value the endpoint knows, not what this model accepts.
    """
    text = str(error_text or '')
    named = _PER_MODEL_REJECTION.search(text)
    values = _SUPPORTED_VALUES.search(text)
    if not named or not values or named.group(1).strip().lower() != str(model_id or '').strip().lower():
        return None
    return learn_efforts(model_id, re.findall(r"'([a-z]+)'", values.group(1)))


def normalize_thinking_level(value, *, default: str = 'off') -> str:
    """Canonical spelling of a thinking level; unknown input becomes `default`."""
    val = str(value or '').strip().lower()
    val = _ALIASES.get(val, val)
    return val if val in THINKING_LEVELS else default


def is_valid_thinking_level(value) -> bool:
    return normalize_thinking_level(value, default='') != ''


def supported_efforts(model_id: str) -> tuple[str, ...]:
    """The effort ladder a model accepts (learned, exact slug, then longest known prefix)."""
    mid = str(model_id or '').strip().lower()
    if mid in _LEARNED_EFFORTS:
        return _LEARNED_EFFORTS[mid]
    if mid.startswith('deepseek-'):
        return DEEPSEEK_EFFORTS
    if mid in CODEX_EFFORTS:
        return CODEX_EFFORTS[mid]
    for known in sorted(CODEX_EFFORTS, key=len, reverse=True):
        if mid.startswith(known + '-'):
            return CODEX_EFFORTS[known]
    return CODEX_DEFAULT_EFFORTS


def clamp_effort(level: str, model_id: str) -> str:
    """Map a requested level onto the model's ladder.

    The highest supported level that does not exceed the request (so ``ultra``
    becomes ``max`` everywhere, and ``max`` becomes ``xhigh`` on gpt-5.5); a
    request below the model's floor gets the floor (``minimal`` → ``low``).
    ``off`` is returned unchanged so callers can keep their "no reasoning
    block" branch.
    """
    lvl = normalize_thinking_level(level, default='medium')
    if lvl == 'off':
        return 'off'
    supported = supported_efforts(model_id)
    rank = {name: i for i, name in enumerate(EFFORT_LADDER)}
    candidates = [s for s in supported if rank[s] <= rank[lvl]]
    return candidates[-1] if candidates else supported[0]


def step_down(level: str, *, floor: str = 'low') -> str:
    """One notch lower on the ladder, never below `floor`; ``off`` stays ``off``."""
    lvl = normalize_thinking_level(level)
    if lvl == 'off':
        return 'off'
    rank = {name: i for i, name in enumerate(EFFORT_LADDER)}
    target = max(rank[lvl] - 1, rank[normalize_thinking_level(floor, default='low')])
    return EFFORT_LADDER[min(target, rank[lvl])]
