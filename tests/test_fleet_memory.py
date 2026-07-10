"""Tests for fleet memory plumbing (working memory + memory engine)."""
import json

from charon.fleet import fleet_memory


def test_update_working_memory_writes_agent_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_memory, 'STATE_DIR', tmp_path)

    fleet_memory._update_working_memory('srv1', 'agent-a', 'refactored the parser')

    path = tmp_path / 'agents' / 'remote:srv1:agent-a' / 'working_memory.json'
    assert path.exists()
    data = json.loads(path.read_text())
    assert data['agent_id'] == 'remote:srv1:agent-a'
    assert data['last_task_summary'] == 'refactored the parser'
    assert data['notes']


def test_harbor_ingest_result_updates_working_memory(tmp_path, monkeypatch):
    # Regression: harbor used to call _update_working_memory with an extra
    # state_dir argument; the resulting TypeError was swallowed, so remote
    # working memory never updated after a voyage completed.
    from charon.fleet import harbor

    monkeypatch.setattr(fleet_memory, 'STATE_DIR', tmp_path)
    state_dir = tmp_path / 'state'

    voyage = {
        'voyage_id': 'v-test-1',
        'manifest': {'server_id': 'srv1', 'agent_name': 'agent-a'},
    }
    result_msg = {
        'voyage_id': 'v-test-1',
        'status': 'completed',
        'result': {'stdout': 'voyage task output'},
    }
    harbor._ingest_result(voyage, result_msg, state_dir)

    path = tmp_path / 'agents' / 'remote:srv1:agent-a' / 'working_memory.json'
    assert path.exists()
    data = json.loads(path.read_text())
    assert data['last_task_summary'] == 'voyage task output'


def test_store_in_memory_engine_uses_state_dir(tmp_path, monkeypatch):
    # Regression: passing STATE_DIR / 'memory.db' to MemoryEngine (which
    # appends 'memory.db' itself) created a nested memory.db/memory.db.
    monkeypatch.setattr(fleet_memory, 'STATE_DIR', tmp_path)

    fleet_memory._store_in_memory_engine('srv1', 'agent-a', 'remote agent summarized activity')

    db_path = tmp_path / 'memory.db'
    assert db_path.is_file()
