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


loader = _load('project_registry_loader_test', DAEMON / 'project_registry_loader.py')
project_registry = _load('project_registry_loader_registry_test', DAEMON / 'project_registry.py')


def test_load_ensure_project_returns_working_function(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    ensure_project = loader.load_ensure_project(str(DAEMON / 'goal_runtime.py'), 'loader_test')
    proj = ensure_project(state_dir, project_root)

    assert proj['id']
    assert project_registry.get_project_by_root(state_dir, project_root)['id'] == proj['id']


def test_load_ensure_project_from_tools_returns_working_function(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    ensure_project = loader.load_ensure_project_from_tools(str(DAEMON / 'tools' / 'memory_tools.py'), 'loader_tools_test')
    proj = ensure_project(state_dir, project_root)

    assert proj['id']
    assert (state_dir / 'projects' / proj['id'] / 'project.json').exists()
