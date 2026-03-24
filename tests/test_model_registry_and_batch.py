"""Tests for model registry and batch orchestrator."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))
sys.path.insert(0, str(ROOT))

from model_registry import load_registry, save_registry, DEFAULT_REGISTRY
from batch_orchestrator import (
    create_batch, get_batch, list_batches, summarize_batch,
    get_next_batch_tasks, mark_batch_task_started,
    mark_batch_task_completed, mark_batch_task_failed,
)
import store_adapter


def setup_function():
    store_adapter.reset_all()


# ── Model registry ─────────────────────────────────────────────────

def test_registry_defaults():
    reg = load_registry(Path('/nonexistent'))
    assert reg['shade_model_mode'] == 'auto'
    assert reg['phase_tier_map']['analysis'] == 'fast'
    assert reg['phase_tier_map']['implementation'] == 'strong'


def test_registry_save_load(tmp_path):
    state_dir = tmp_path / 'state'
    reg = load_registry(state_dir)
    reg['shade_model_mode'] = 'fixed'
    reg['shade_model'] = 'gpt-4o-mini'
    reg['shade_provider'] = 'openai'
    save_registry(state_dir, reg)

    loaded = load_registry(state_dir)
    assert loaded['shade_model_mode'] == 'fixed'
    assert loaded['shade_model'] == 'gpt-4o-mini'
    assert loaded['shade_provider'] == 'openai'


def test_registry_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_SHADE_MODEL', 'gemini-flash')
    reg = load_registry(tmp_path / 'state')
    assert reg['shade_model_mode'] == 'fixed'
    assert reg['shade_model'] == 'gemini-flash'


# ── Batch orchestrator ──────────────────────────────────────────────

def test_create_batch(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(
        state_dir,
        parent_agent_id='AG-001',
        project='/tmp/proj',
        goal='Generate 5 images',
        tasks=[
            {'instruction': 'Generate image 1', 'title': 'Image 1'},
            {'instruction': 'Generate image 2', 'title': 'Image 2'},
            {'instruction': 'Generate image 3', 'title': 'Image 3'},
            {'instruction': 'Generate image 4', 'title': 'Image 4'},
            {'instruction': 'Generate image 5', 'title': 'Image 5'},
        ],
        max_concurrent=3,
    )
    assert batch['id'].startswith('batch-')
    assert batch['total'] == 5
    assert batch['max_concurrent'] == 3
    assert batch['status'] == 'pending'
    assert len(batch['tasks']) == 5
    assert all(t['status'] == 'pending' for t in batch['tasks'])


def test_get_batch(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(state_dir, parent_agent_id='AG-001',
                          project='/', goal='Test', tasks=[{'instruction': 'do'}])
    loaded = get_batch(state_dir, batch['id'])
    assert loaded is not None
    assert loaded['id'] == batch['id']


def test_list_batches(tmp_path):
    state_dir = tmp_path / 'state'
    create_batch(state_dir, parent_agent_id='AG-001', project='/', goal='A',
                  tasks=[{'instruction': 'a'}])
    create_batch(state_dir, parent_agent_id='AG-001', project='/', goal='B',
                  tasks=[{'instruction': 'b'}])

    all_batches = list_batches(state_dir)
    assert len(all_batches) == 2

    pending = list_batches(state_dir, status='pending')
    assert len(pending) == 2


def test_get_next_batch_tasks_respects_concurrency(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(
        state_dir, parent_agent_id='AG-001', project='/', goal='Test',
        tasks=[{'instruction': f'task {i}'} for i in range(10)],
        max_concurrent=3,
    )

    # Should get at most 3
    next_tasks = get_next_batch_tasks(state_dir, batch['id'], count=10)
    assert len(next_tasks) == 3

    # Mark 2 as in_progress
    mark_batch_task_started(state_dir, batch['id'], next_tasks[0]['id'], 'SH-001')
    mark_batch_task_started(state_dir, batch['id'], next_tasks[1]['id'], 'SH-002')

    # Now should only get 1 more (3 max - 2 in_progress = 1)
    next_tasks2 = get_next_batch_tasks(state_dir, batch['id'], count=10)
    assert len(next_tasks2) == 1


def test_batch_task_completion(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(
        state_dir, parent_agent_id='AG-001', project='/', goal='Test',
        tasks=[
            {'instruction': 'task 1'},
            {'instruction': 'task 2'},
        ],
        max_concurrent=5,
    )

    t1, t2 = batch['tasks'][0]['id'], batch['tasks'][1]['id']

    mark_batch_task_started(state_dir, batch['id'], t1, 'SH-001')
    mark_batch_task_completed(state_dir, batch['id'], t1, 'Done task 1')

    updated = get_batch(state_dir, batch['id'])
    assert updated['completed_count'] == 1
    assert updated['status'] == 'running'

    mark_batch_task_started(state_dir, batch['id'], t2, 'SH-002')
    mark_batch_task_completed(state_dir, batch['id'], t2, 'Done task 2')

    final = get_batch(state_dir, batch['id'])
    assert final['completed_count'] == 2
    assert final['status'] == 'completed'


def test_batch_task_failure(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(
        state_dir, parent_agent_id='AG-001', project='/', goal='Test',
        tasks=[
            {'instruction': 'task 1'},
            {'instruction': 'task 2'},
        ],
    )

    t1, t2 = batch['tasks'][0]['id'], batch['tasks'][1]['id']

    mark_batch_task_started(state_dir, batch['id'], t1, 'SH-001')
    mark_batch_task_failed(state_dir, batch['id'], t1, 'Connection failed')

    mark_batch_task_started(state_dir, batch['id'], t2, 'SH-002')
    mark_batch_task_completed(state_dir, batch['id'], t2, 'Done')

    final = get_batch(state_dir, batch['id'])
    assert final['status'] == 'partial'  # not all succeeded
    assert final['failed_count'] == 1
    assert final['completed_count'] == 1


def test_summarize_batch(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(
        state_dir, parent_agent_id='AG-001', project='/', goal='Test',
        tasks=[{'instruction': f'task {i}'} for i in range(5)],
    )

    summary = summarize_batch(batch)
    assert 'pending' in summary
    assert '5' in summary


def test_batch_with_constraints(tmp_path):
    state_dir = tmp_path / 'state'
    batch = create_batch(
        state_dir, parent_agent_id='AG-001', project='/', goal='Generate',
        tasks=[
            {'instruction': 'Make image', 'title': 'Img 1'},
        ],
        constraints=['Output must be PNG', 'Max 1024x1024'],
    )

    task = batch['tasks'][0]
    assert 'Output must be PNG' in task['constraints']
    assert 'Max 1024x1024' in task['constraints']
