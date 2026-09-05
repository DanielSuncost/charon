"""The system-map skill as a Charon primitive: the RFC ``skill`` record, the
SKILL.md installer (procedural memory, see charon.tools.skills_tool), and the
path resolution the ``SystemMap`` tool uses to run ``skills/system-map/*.py``
in-process.

The executable contract lives in ``skills/system-map/`` at the repo root
(stdlib only); Acheron ships a JS port with the same contract and both are
tested against the same fixture repo.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

SKILL_NAME = 'system-map'
SKILL_VERSION = '1'
SKILL_CAPABILITIES = ['map.extract', 'map.validate', 'map.query']
SKILL_DESCRIPTION = (
    'A living, code-anchored model of a project: a declared system-map.json (subsystems → '
    'components → code globs, interfaces, dependencies, invariants) plus derived facts '
    'extracted from the code (import relations with evidence, sizes, last change, gaps, '
    'conflicts). Declared beats inferred: extraction reports disagreements, never edits the map.'
)

_FALLBACK_SKILL_MD = """# system-map — a living, code-anchored model of the project

Declared half: `system-map.json` (subsystems → components → code globs/anchors, interfaces,
depends_on, invariants). Derived half: `extract.py` facts from the code (relations with
file:line evidence, sizes, last change, gaps, conflicts). Declared beats inferred.

    python3 skills/system-map/validate.py --root <dir>
    python3 skills/system-map/extract.py  --root <dir> [--out derived.json]
    python3 skills/system-map/query.py    --root <dir> [component]

Answer "how does X work?" from `query`: name the component, cite its `code` locators and
invariants, state freshness (extracted_at / source_revision), and say what is derived vs
declared. Gaps and undeclared dependencies are proposals, never silent edits.
"""


def repo_root() -> Path:
    """The Charon checkout this module lives in (``src/charon/workspace`` → repo root)."""
    return Path(__file__).resolve().parents[3]


def skills_dir() -> Path:
    """Directory holding ``skills/system-map`` (``$CHARON_SKILLS_DIR`` overrides the checkout)."""
    env = os.environ.get('CHARON_SKILLS_DIR')
    base = Path(env) if env else repo_root() / 'skills'
    return base / SKILL_NAME


def skill_md_text() -> str:
    path = skills_dir() / 'SKILL.md'
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return _FALLBACK_SKILL_MD


def skill_path(state_dir: Path) -> Path:
    return Path(state_dir) / 'skills' / SKILL_NAME / 'SKILL.md'


def install_system_map_skill(state_dir: Path, *, overwrite: bool = False) -> Path:
    """Write .charon_state/skills/system-map/SKILL.md (kept if it exists unless overwrite)."""
    path = skill_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not path.exists():
        path.write_text(skill_md_text(), encoding='utf-8')
    return path


def load_skill_module(name: str = 'mapcore') -> ModuleType:
    """Import ``skills/system-map/<name>.py`` by path (no package, stdlib only)."""
    path = skills_dir() / f'{name}.py'
    if not path.is_file():
        raise FileNotFoundError(f'system-map skill not found at {path}')
    key = f'charon_skill_system_map_{name}'
    cached = sys.modules.get(key)
    if cached is not None and getattr(cached, '__file__', None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(key, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load {path}')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _iso(now=None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def skill_record(*, workspace_id: str = 'workspace.charon.local', now=None) -> dict:
    """The RFC ``skill`` record (docs/contracts/workspace.schema.json ``$defs/skill``)."""
    ts = _iso(now)
    return {
        'record_type': 'skill',
        'id': f'skill.{SKILL_NAME}',
        'workspace_id': workspace_id,
        'revision': 1,
        'created_at': ts,
        'updated_at': ts,
        'labels': {},
        'provenance': [],
        'extensions': {'skill_dir': str(skills_dir()), 'tool': 'SystemMap'},
        'name': SKILL_NAME,
        'version': SKILL_VERSION,
        'description': SKILL_DESCRIPTION,
        'status': 'active',
        'capabilities': list(SKILL_CAPABILITIES),
        'invocation': {
            'kind': 'command',
            'entrypoint': 'python3 skills/system-map/extract.py',
            'side_effect_level': 'read',
            'input_contract': {
                'args': ['--root <dir>', '--map system-map.json', '--out derived.json', '--no-git'],
                'declared_map_schema': 'docs/contracts/system-map.schema.json',
            },
            'output_contract': {
                'derived_facts': ['extracted_at', 'source_revision', 'extractor', 'components', 'relations', 'gaps', 'conflicts', 'validation'],
            },
        },
        'policy_ids': [],
    }
