from charon.infra import project_registry
from charon.context import system_prompt_builder
from charon.context import context_transfer


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
