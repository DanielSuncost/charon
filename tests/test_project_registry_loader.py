from pathlib import Path

from charon.infra import project_registry
from charon.infra import project_registry_loader as loader

ROOT = Path(__file__).resolve().parents[1]
SRC_CHARON = ROOT / 'src' / 'charon'


def test_load_ensure_project_returns_working_function(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    ensure_project = loader.load_ensure_project(str(SRC_CHARON / 'agents' / 'goal_runtime.py'), 'loader_test')
    proj = ensure_project(state_dir, project_root)

    assert proj['id']
    assert project_registry.get_project_by_root(state_dir, project_root)['id'] == proj['id']


def test_load_ensure_project_from_tools_returns_working_function(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    ensure_project = loader.load_ensure_project_from_tools(str(SRC_CHARON / 'tools' / 'memory_tools.py'), 'loader_tools_test')
    proj = ensure_project(state_dir, project_root)

    assert proj['id']
    assert (state_dir / 'projects' / proj['id'] / 'project.json').exists()
