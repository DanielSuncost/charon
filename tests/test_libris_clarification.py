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


def test_controller_waits_for_running_coordinator(tmp_path, monkeypatch):
    """Regression: the controller must wait for a RUNNING coordinator's scouting
    pass (minutes-long LLM run), not park at clarification after seconds. Topics
    saved mid-scout must trigger fanout, not awaiting_clarification."""
    project_root = tmp_path / 'proj'
    project_root.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / 'state'

    op = runtime_mod.init_operation(
        state_dir, project_root,
        prompt='Investigate agent memory datasets',
        coordinator_agent_id='AG-COORD-2',
    )
    op_id = op['operation_id']

    # Coordinator stays 'running' the whole time (registry stub)
    monkeypatch.setattr(agents_mod, '_agent_status', lambda _aid: 'running')

    # Hermetic fan-out: the controller lazily imports these and silently skips
    # them on ImportError; when a prior test has imported the real modules,
    # they spawn shade threads that keep writing into the tmp operation dir and
    # race this test's reads. Stub them so the test is deterministic either way.
    import types as _types
    orch = _types.ModuleType('libris_orchestrator')
    orch.gather_source_leads_for_topic = lambda *a, **k: []
    orch.spawn_topic_procurement_shades = lambda *a, **k: []
    orch.wait_for_procurement_contracts = lambda *a, **k: []
    orch.build_procurement_summary_markdown = lambda *a, **k: ''
    monkeypatch.setitem(sys.modules, 'libris_orchestrator', orch)
    spec_mod = _types.ModuleType('libris_specialists')
    spec_mod.spawn_topic_claim_extraction_shades = lambda *a, **k: []
    spec_mod.wait_for_claim_extraction_contracts = lambda *a, **k: []
    spec_mod.ingest_claim_extraction_contracts = lambda *a, **k: []
    spec_mod.spawn_topic_contradiction_check_shades = lambda *a, **k: []
    spec_mod.wait_for_contradiction_check_contracts = lambda *a, **k: []
    spec_mod.ingest_contradiction_check_contracts = lambda *a, **k: []
    monkeypatch.setitem(sys.modules, 'libris_specialists', spec_mod)
    conv = _types.ModuleType('libris_convergence')
    conv.should_request_additional_revision = lambda *a, **k: {
        'should_revise': False, 'reasons': ['quality_good_enough'], 'metrics': {}}
    monkeypatch.setitem(sys.modules, 'libris_convergence', conv)

    spawned = []
    monkeypatch.setattr(
        agents_mod, 'spawn_libris_role',
        lambda *a, **k: (spawned.append(k), {'id': f'AG-FAKE-{len(spawned)}'})[1])

    calls = {'n': 0}

    def fake_sleep(_s):
        calls['n'] += 1
        if calls['n'] == 3:
            # coordinator "finishes scouting" only after several waits —
            # the old 9-second logic would already have parked by now
            runtime_mod.save_candidate_topics(
                state_dir, project_root, op_id,
                topics=[{'title': 'IRC disentanglement corpora',
                         'why_interesting': 'gold reply structure',
                         'recommended_action': 'deep_research'}])
        if calls['n'] >= 8:
            # exit the supervisor loop cleanly once fanout has happened
            runtime_mod.request_stop(state_dir, project_root, op_id, reason='test done')

    monkeypatch.setattr(agents_mod.time, 'sleep', fake_sleep)

    agents_mod._run_operation_controller(
        state_dir, project_root, op_id,
        'Investigate agent memory datasets',
        {'id': 'AG-COORD-2', 'name': 'coord'}, 3,
    )

    op_state = runtime_mod.get_operation_state(state_dir, project_root, op_id)
    assert op_state['status'] != 'awaiting_clarification'
    assert spawned, 'researcher fanout should have happened'
    assert spawned[0].get('role') == 'researcher'
    assert [t.get('slug') for t in op_state.get('topics') or []]
