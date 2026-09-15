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
does not read the Codex CLI's files at runtime — so a new model needs a row
here before its upper levels stop being clamped.
"""
from __future__ import annotations

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


def normalize_thinking_level(value, *, default: str = 'off') -> str:
    """Canonical spelling of a thinking level; unknown input becomes `default`."""
    val = str(value or '').strip().lower()
    val = _ALIASES.get(val, val)
    return val if val in THINKING_LEVELS else default


def is_valid_thinking_level(value) -> bool:
    return normalize_thinking_level(value, default='') != ''


def supported_efforts(model_id: str) -> tuple[str, ...]:
    """The effort ladder a Codex model accepts (exact slug, then longest known prefix)."""
    mid = str(model_id or '').strip().lower()
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
