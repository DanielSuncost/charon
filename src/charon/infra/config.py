"""Central registry of CHARON_* environment variables.

Every environment knob the daemon, tools, and TUI respond to is defined
here as one typed accessor function. Accessors read ``os.environ`` at
CALL time — never cache at import time — because tests and the launcher
set these variables after modules are imported (``monkeypatch.setenv``,
subprocess ``env=...``).

This module is the bottom of the dependency graph: it must import
nothing from ``charon`` except the standard library.

See src/charon/README.md ("Configuration") for the full variable table.
"""
from __future__ import annotations

import os
from pathlib import Path

# Default OpenAI-compatible endpoint for local inference (LM Studio et al).
DEFAULT_LOCAL_BASE_URL = 'http://127.0.0.1:1234/v1'

# Default sentence-transformers embedding model.
DEFAULT_EMBED_MODEL = 'BAAI/bge-base-en-v1.5'


def _get_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


# ── Core loop / persistence ──────────────────────────────────────────────────

def no_sqlite() -> bool:
    """CHARON_NO_SQLITE (default '0'): '1' disables the SQLite store and
    falls back to JSON-file persistence. Any other value keeps SQLite on."""
    return os.environ.get('CHARON_NO_SQLITE', '0') == '1'


def stdout_events() -> bool:
    """CHARON_STDOUT_EVENTS (default '1'): '1' mirrors loop events to stdout
    as JSONL. Tests set '0' to keep subprocess output clean."""
    return os.environ.get('CHARON_STDOUT_EVENTS', '1') == '1'


def debug_trace() -> bool:
    """CHARON_DEBUG_TRACE (default '0'): '1' enables the high-volume JSONL
    trace written to <state-dir>/debug.log."""
    return os.environ.get('CHARON_DEBUG_TRACE', '0') == '1'


def state_dir() -> Path | None:
    """CHARON_STATE_DIR (no default): state directory override.

    Returns the raw Path or None when unset. Call sites apply their own
    fallbacks (charon_loop: './.charon_state'; diagnostics and
    provider_bridge: '~/.charon_state') — see README for the discrepancy.
    """
    raw = os.environ.get('CHARON_STATE_DIR')
    return Path(raw) if raw else None


def stop_file() -> str:
    """CHARON_STOP_FILE (default './CHARON_STOP'): path of the sentinel file
    whose existence stops the loop."""
    return os.environ.get('CHARON_STOP_FILE', './CHARON_STOP')


def max_consec_fail() -> int:
    """CHARON_MAX_CONSEC_FAIL (default 5): consecutive cycle failures before
    the loop aborts."""
    return _get_int('CHARON_MAX_CONSEC_FAIL', 5)


def loop_sleep() -> float:
    """CHARON_LOOP_SLEEP (default 2.0): seconds slept between loop cycles."""
    return float(os.environ.get('CHARON_LOOP_SLEEP', '2.0'))


def max_cycles() -> int:
    """CHARON_MAX_CYCLES (default 0): stop after N cycles; 0 = run forever."""
    return _get_int('CHARON_MAX_CYCLES', 0)


def stale_in_progress_sec() -> int:
    """CHARON_STALE_IN_PROGRESS_SEC (default 60): seconds before an
    in-progress task with no heartbeat is considered stale and requeued."""
    return _get_int('CHARON_STALE_IN_PROGRESS_SEC', 60)


def heartbeat_interval() -> int:
    """CHARON_HEARTBEAT_INTERVAL (default 30): loop cycles between heartbeat
    events."""
    return _get_int('CHARON_HEARTBEAT_INTERVAL', 30)


# ── Agents ───────────────────────────────────────────────────────────────────

def require_tmux() -> bool:
    """CHARON_REQUIRE_TMUX (default '1'): '1' means newly created agents
    require a tmux session; any other value relaxes the requirement."""
    return os.environ.get('CHARON_REQUIRE_TMUX', '1') == '1'


def shade_require_tmux() -> bool:
    """CHARON_SHADE_REQUIRE_TMUX (default '0'): '1' makes shade contract
    agents require tmux (opt-in, unlike CHARON_REQUIRE_TMUX)."""
    return os.environ.get('CHARON_SHADE_REQUIRE_TMUX', '0') == '1'


def agent_planner() -> str | None:
    """CHARON_AGENT_PLANNER (no default): 'heuristic' or 'llm' forces the
    planner mode; unset/empty defers to onboarding config."""
    val = os.environ.get('CHARON_AGENT_PLANNER', '').strip().lower()
    return val or None


def agent_shell_timeout() -> int:
    """CHARON_AGENT_SHELL_TIMEOUT (default 45): timeout in seconds for shell
    actions executed by agent runtimes."""
    return _get_int('CHARON_AGENT_SHELL_TIMEOUT', 45)


def spec_window() -> int:
    """CHARON_SPEC_WINDOW (default 10): recent task summaries considered when
    deriving an agent's soft specialization."""
    return _get_int('CHARON_SPEC_WINDOW', 10)


def spec_min_tasks() -> int:
    """CHARON_SPEC_MIN_TASKS (default 3): minimum completed tasks before a
    specialization label is generated."""
    return _get_int('CHARON_SPEC_MIN_TASKS', 3)


def spec_interval() -> int:
    """CHARON_SPEC_INTERVAL (default 300): seconds between specialization
    refreshes in the loop."""
    return _get_int('CHARON_SPEC_INTERVAL', 300)


def autonomous_override() -> bool | None:
    """CHARON_AUTONOMOUS (no default): '1'/'true'/'on' forces autonomous mode
    on, '0'/'false'/'off' forces it off, anything else leaves the config-file
    value untouched (returns None)."""
    val = os.environ.get('CHARON_AUTONOMOUS', '').lower()
    if val in ('1', 'true', 'on'):
        return True
    if val in ('0', 'false', 'off'):
        return False
    return None


# ── Approvals ────────────────────────────────────────────────────────────────

def skip_approval() -> bool:
    """CHARON_SKIP_APPROVAL (default '0'): '1'/'true'/'yes' disables all tool
    approval checks."""
    return os.environ.get('CHARON_SKIP_APPROVAL', '0') in ('1', 'true', 'yes')


# ── Browser / tools ──────────────────────────────────────────────────────────

def browser_headless() -> bool:
    """CHARON_BROWSER_HEADLESS (default '1'): browser launches headless
    unless the value is exactly '0'. Fallback used by the Browser and X tools
    when browser_settings resolution is unavailable."""
    return os.environ.get('CHARON_BROWSER_HEADLESS', '1') != '0'


def browser_headless_raw() -> str:
    """CHARON_BROWSER_HEADLESS raw value ('' when unset). browser_settings
    treats this as a legacy tri-state: '' = no opinion (fall through to the
    hardcoded default), '0' = show the browser, anything else = hide.
    Inverted naming is historical — preserved exactly."""
    return os.environ.get('CHARON_BROWSER_HEADLESS', '')


def x_profile_dir() -> Path | None:
    """CHARON_X_PROFILE_DIR (no default): override for the persistent
    Chromium profile used by the X tool. Empty/unset returns None
    (call site falls back to <state_dir>/browser/x)."""
    custom = os.environ.get('CHARON_X_PROFILE_DIR', '').strip()
    return Path(custom).expanduser() if custom else None


def searxng_url() -> str:
    """CHARON_SEARXNG_URL (default ''): base URL of a self-hosted SearXNG
    instance for web search. Empty disables the SearXNG provider."""
    return os.environ.get('CHARON_SEARXNG_URL', '').strip()


# ── Memory / embeddings ──────────────────────────────────────────────────────

def embed_model() -> str:
    """CHARON_EMBED_MODEL (default 'BAAI/bge-base-en-v1.5'):
    sentence-transformers model used for memory embeddings."""
    return os.environ.get('CHARON_EMBED_MODEL', DEFAULT_EMBED_MODEL)


def embed_backend() -> str:
    """CHARON_EMBED_BACKEND (default 'worker'): 'worker' uses the embedding
    worker subprocess; 'local' loads the model in-process (tests use this)."""
    return os.environ.get('CHARON_EMBED_BACKEND', 'worker').strip().lower()


def embed_device() -> str | None:
    """CHARON_EMBED_DEVICE (no default): torch device for the embedding model
    ('cpu', 'cuda', 'mps', ...). Empty/unset returns None (auto-detect)."""
    val = os.environ.get('CHARON_EMBED_DEVICE', '').strip()
    return val or None


def embed_idle_secs() -> int:
    """CHARON_EMBED_IDLE_SECS (default 120, floor 15): idle seconds before
    the embedding worker shuts itself down."""
    return max(15, int(os.environ.get('CHARON_EMBED_IDLE_SECS', '120') or '120'))


def consolidation_model() -> str | None:
    """CHARON_CONSOLIDATION_MODEL (no default): model tier override for
    memory consolidation. Unset/empty returns None."""
    return os.environ.get('CHARON_CONSOLIDATION_MODEL') or None


def consolidation_interval() -> int | None:
    """CHARON_CONSOLIDATION_INTERVAL (no default): heartbeats between
    consolidation scans. Unset/empty returns None."""
    raw = os.environ.get('CHARON_CONSOLIDATION_INTERVAL')
    return int(raw) if raw else None


def consolidation_disabled() -> bool:
    """CHARON_CONSOLIDATION_ENABLED (no default): only the literal value
    'false' (case-insensitive) disables consolidation; anything else is a
    no-op. Note the asymmetry: there is no env value that force-enables."""
    return os.environ.get('CHARON_CONSOLIDATION_ENABLED', '').lower() == 'false'


# ── Providers / models ───────────────────────────────────────────────────────

def local_base_url() -> str | None:
    """CHARON_LOCAL_BASE_URL (no default): OpenAI-compatible base URL for the
    'local' provider. Empty/unset returns None; most call sites then try
    CHARON_LMSTUDIO_BASE_URL and finally DEFAULT_LOCAL_BASE_URL."""
    val = (os.environ.get('CHARON_LOCAL_BASE_URL') or '').strip()
    return val or None


def lmstudio_base_url() -> str | None:
    """CHARON_LMSTUDIO_BASE_URL (no default): legacy alias for the local
    base URL, still honored as a fallback after CHARON_LOCAL_BASE_URL.
    Empty/unset returns None."""
    val = (os.environ.get('CHARON_LMSTUDIO_BASE_URL') or '').strip()
    return val or None


def local_api_key() -> str:
    """CHARON_LOCAL_API_KEY (default 'not-needed'): API key sent to the local
    provider endpoint."""
    return os.environ.get('CHARON_LOCAL_API_KEY', 'not-needed')


def local_model() -> str | None:
    """CHARON_LOCAL_MODEL (no default): model id override for the local
    provider (a 'lmstudio/' prefix is stripped by call sites). Empty/unset
    returns None."""
    val = os.environ.get('CHARON_LOCAL_MODEL', '').strip()
    return val or None


def shade_model_mode() -> str | None:
    """CHARON_SHADE_MODEL_MODE (no default): overrides the model-selection
    mode for shade agents in the model registry. Empty/unset returns None."""
    val = os.environ.get('CHARON_SHADE_MODEL_MODE', '').strip()
    return val or None


def shade_model() -> str | None:
    """CHARON_SHADE_MODEL (no default): pins shades to a fixed model (also
    forces shade_model_mode='fixed'). Empty/unset returns None."""
    val = os.environ.get('CHARON_SHADE_MODEL', '').strip()
    return val or None


# ── TUI launcher ─────────────────────────────────────────────────────────────

def requested_provider() -> str:
    """CHARON_PROVIDER (default ''): provider requested at TUI launch
    (e.g. 'local', 'claude-code'). Empty means use onboarding config."""
    return os.environ.get('CHARON_PROVIDER', '').strip()


def requested_resume() -> str:
    """CHARON_RESUME (default ''): agent id (or 'latest') whose conversation
    the TUI should resume at launch. Empty means start fresh."""
    return os.environ.get('CHARON_RESUME', '').strip()


def requested_agent() -> str:
    """CHARON_AGENT (default ''): agent id or name the TUI session should
    bind to at launch. Empty means an unbound fresh session."""
    return os.environ.get('CHARON_AGENT', '').strip()
