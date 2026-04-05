import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / 'apps' / 'core-daemon'
sys.path.insert(0, str(DAEMON))
sys.path.insert(0, str(ROOT))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


project_registry = _load('project_registry_pk_test', DAEMON / 'project_registry.py')
system_prompt_builder = _load('system_prompt_builder_pk_test', DAEMON / 'system_prompt_builder.py')
context_transfer = _load('context_transfer_pk_test', DAEMON / 'context_transfer.py')


def test_system_prompt_builder_reads_canonical_project_knowledge(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    proj = project_registry.ensure_project(state_dir, project_root)
    kp = state_dir / 'projects' / proj['id'] / 'KNOWLEDGE.md'
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text('Canonical knowledge entry', encoding='utf-8')

    rendered = system_prompt_builder._build_project_knowledge(state_dir, str(project_root))
    assert 'PROJECT KNOWLEDGE' in rendered
    assert 'Canonical knowledge entry' in rendered


def test_context_transfer_prefers_canonical_project_knowledge(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    proj = project_registry.ensure_project(state_dir, project_root)
    canonical = state_dir / 'projects' / proj['id'] / 'KNOWLEDGE.md'
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text('Canonical context knowledge', encoding='utf-8')

    legacy = project_root / '.charon' / 'PROJECT_KNOWLEDGE.md'
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('Legacy knowledge', encoding='utf-8')

    loaded = context_transfer._load_project_knowledge(state_dir, project_root)
    assert loaded == 'Canonical context knowledge'
