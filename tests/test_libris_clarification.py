from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
CORE_DAEMON = ROOT / 'apps' / 'core-daemon'
if str(CORE_DAEMON) not in sys.path:
    sys.path.insert(0, str(CORE_DAEMON))
TOOLS_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / '__init__.py'
CL_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / 'clarify_tool.py'
RUNTIME_PATH = ROOT / 'apps' / 'core-daemon' / 'libris_runtime.py'
AGENTS_PATH = ROOT / 'apps' / 'core-daemon' / 'libris_agents.py'


spec_tools = importlib.util.spec_from_file_location('tools', TOOLS_PATH)
tools_mod = importlib.util.module_from_spec(spec_tools)
sys.modules['tools'] = tools_mod
spec_tools.loader.exec_module(tools_mod)

spec_cl = importlib.util.spec_from_file_location('clarify_tool', CL_PATH)
cl_mod = importlib.util.module_from_spec(spec_cl)
sys.modules['clarify_tool'] = cl_mod
spec_cl.loader.exec_module(cl_mod)

spec_runtime = importlib.util.spec_from_file_location('libris_runtime', RUNTIME_PATH)
runtime_mod = importlib.util.module_from_spec(spec_runtime)
sys.modules['libris_runtime'] = runtime_mod
spec_runtime.loader.exec_module(runtime_mod)

spec_agents = importlib.util.spec_from_file_location('libris_agents', AGENTS_PATH)
agents_mod = importlib.util.module_from_spec(spec_agents)
sys.modules['libris_agents'] = agents_mod
spec_agents.loader.exec_module(agents_mod)


def test_missing_candidate_topics_requests_clarification(tmp_path, monkeypatch):
    project_root = tmp_path / 'proj'
    project_root.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / 'state'

    op = runtime_mod.init_operation(
        state_dir,
        project_root,
        prompt='Investigate self-distillation in machine learning',
        coordinator_agent_id='AG-COORD',
    )

    monkeypatch.setattr(agents_mod.time, 'sleep', lambda _s: None)

    agents_mod._run_operation_controller(
        state_dir,
        project_root,
        op['operation_id'],
        'Investigate self-distillation in machine learning',
        {'id': 'AG-COORD', 'name': 'coord'},
        3,
    )

    op_state = runtime_mod.get_operation_state(state_dir, project_root, op['operation_id'])
    assert op_state['status'] == 'awaiting_clarification'
    assert op_state['candidate_topics'] == []
    assert op_state['selected_topic_ids'] == []

    clar_ctx = tools_mod.ToolContext(project_root=project_root, agent_id='AG-TEST', state_dir=state_dir)
    pending = cl_mod.execute_clarify({'action': 'list'}, clar_ctx)
    items = pending.details['items']
    assert len(items) == 1
    row = items[0]
    assert 'self-distillation' in row['question'].lower()
    assert len(row['choices']) >= 3
    assert any('self-distillation' in c.lower() for c in row['choices'])

    events = [e for e in op_state['events_tail'] if e.get('type') == 'clarification_requested']
    assert events
