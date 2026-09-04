from __future__ import annotations

from pathlib import Path

import pytest

from backend import common
from backend.dashboard import DashboardMixin, _project_libris_room
from chat_backend import ChatBackend
from charon.libris import libris_durable, libris_runtime


@pytest.fixture
def backend() -> ChatBackend:
    """Build the command router without starting backend scheduler threads."""
    instance = ChatBackend.__new__(ChatBackend)
    instance._pending_provider_switch = None
    instance._pending_fleet_setup = None
    instance._pending_libris_intake = None
    instance._active_agent_id = None
    instance._last_libris_operation_id = ''
    instance._tracked_libris_operation_ids = []
    instance._announced_libris_completions = set()
    return instance


def _ready_swarm(tmp_path: Path, operation_id: str = 'rop-ready') -> dict:
    report = (tmp_path / 'delivery' / 'report.html').resolve()
    summary = (tmp_path / 'delivery' / 'executive-summary.md').resolve()
    return {
        'operation_id': operation_id,
        'status': 'delivered',
        'topics': [
            {
                'title': 'Routing policies',
                'status': 'checkpointed',
                'phase': 'judging',
            },
        ],
        'delivery_manifest': {
            'status': 'ready',
            'ready': True,
            'topic_count': 1,
            'primary_artifact': {
                'label': 'Research report',
                'path': str(report),
                'media_type': 'text/html',
            },
            'artifacts': [
                {
                    'label': 'Research report',
                    'path': str(report),
                    'media_type': 'text/html',
                },
                {
                    'label': 'Executive summary',
                    'path': str(summary),
                    'media_type': 'text/markdown',
                },
            ],
        },
    }


def test_dashboard_libris_room_preserves_lifecycle_graph_projections(tmp_path):
    operation = libris_runtime.init_operation(
        tmp_path,
        tmp_path,
        prompt='Research graph projection',
        coordinator_agent_id='AG-coordinator',
    )
    topic = libris_runtime.init_topic(
        tmp_path,
        tmp_path,
        operation['operation_id'],
        title='Projection topic',
        researcher_agent_id='AG-researcher',
    )
    libris_runtime.emit_agent_comm(
        tmp_path,
        tmp_path,
        operation['operation_id'],
        from_agent_id='AG-coordinator',
        to_agent_id='AG-researcher',
        from_role='coordinator',
        to_role='researcher',
        topic_slug=topic['slug'],
        message_kind='assignment',
        summary='Research the projection boundary.',
    )
    swarm = libris_runtime.get_libris_swarm_state(
        tmp_path,
        tmp_path,
        operation['operation_id'],
    )

    room = _project_libris_room(operation, swarm, tmp_path)

    assert room['lifecycle'] == swarm['lifecycle']
    assert room['lifecycle_graph'] == swarm['lifecycle_graph']
    assert room['workflow_graph'] == swarm['workflow_graph']
    assert room['nodes'] == swarm['nodes']
    assert room['edges'] == swarm['edges']
    assert room['nodes']
    assert room['edges']
    assert room['lifecycle_graph']['current_state'] == 'running'


def test_libris_prompt_emits_structured_intake(backend, monkeypatch):
    emitted = []
    monkeypatch.setattr(common, 'emit', emitted.append)

    backend.handle_command(
        '/libris compare graph schedulers and stop after 2 hours',
        'req-intake',
    )

    assert len(emitted) == 1
    event = emitted[0]
    assert event['type'] == 'libris_intake'
    assert event['request_id'] == 'req-intake'
    assert event['prompt'] == 'compare graph schedulers and stop after 2 hours'
    assert event['stop_condition'] == 'stop after 2 hours'
    assert len(event['options']) == 3
    assert all(isinstance(option, str) for option in event['options'])


def test_libris_launch_selects_and_starts_in_one_command(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    captured = {}
    backend._active_agent_id = 'parent-agent'
    backend._pending_libris_intake = {
        'prompt': 'Compare graph schedulers',
        'goal_options': ['Favor novelty', 'Favor measured reliability'],
        'selected_goal': '',
        'stop_condition': 'stop after 2 hours',
    }

    def fake_start(state_dir, project_root, **kwargs):
        captured['state_dir'] = state_dir
        captured['project_root'] = project_root
        captured.update(kwargs)
        return {
            'operation': {'operation_id': 'op-1'},
            'coordinator': {'id': 'coord-1', 'name': 'Coordinator'},
            'durable_op_id': 'durable-1',
        }

    monkeypatch.setattr(common, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(libris_durable, 'start_durable_libris_research', fake_start)
    monkeypatch.setattr(backend, '_libris_project_root', lambda: str(tmp_path))
    monkeypatch.setattr(
        backend,
        '_get_refresh_payload',
        lambda: {'inter_agent_rooms': [{'id': 'libris-op-1'}]},
    )

    backend.handle_command('/libris launch 2', 'req-launch')

    assert captured['prompt'] == (
        'Compare graph schedulers\n\n'
        'Research goal standard: Favor measured reliability\n\n'
        'Stop condition: stop after 2 hours'
    )
    assert captured['parent_agent_id'] == 'parent-agent'
    assert captured['budget'] == {'max_wall_hours': 2}
    assert backend._pending_libris_intake is None
    assert backend._last_libris_operation_id == 'op-1'
    assert backend._tracked_libris_operation_ids == ['op-1']
    assert [event['type'] for event in emitted] == [
        'status',
        'refresh',
        'libris_started',
    ]
    assert emitted[1]['payload']['inter_agent_rooms'] == [
        {'id': 'libris-op-1'},
    ]
    assert emitted[-1]['operation_id'] == 'op-1'
    assert 'Press F4 to watch the live graph.' in emitted[0]['message']
    assert '/libris status' in emitted[0]['message']


def test_libris_launch_reopens_intake_after_start_failure(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    backend._pending_libris_intake = {
        'prompt': 'Compare graph schedulers',
        'goal_options': ['Favor novelty', 'Favor measured reliability'],
        'selected_goal': '',
        'stop_condition': '',
    }

    def fail_start(*args, **kwargs):
        raise RuntimeError('provider unavailable')

    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(libris_durable, 'start_durable_libris_research', fail_start)
    monkeypatch.setattr(backend, '_libris_project_root', lambda: str(tmp_path))

    backend.handle_command('/libris launch 1', 'req-retry')

    assert backend._pending_libris_intake['selected_goal'] == 'Favor novelty'
    assert [event['type'] for event in emitted] == ['error', 'libris_intake']
    assert emitted[0]['error'] == (
        'Failed to start Libris research: provider unavailable'
    )
    assert emitted[1]['request_id'] == 'req-retry'
    assert emitted[1]['options'] == [
        'Favor novelty',
        'Favor measured reliability',
    ]


def test_libris_launch_rejects_invalid_option_without_starting(
    backend,
    monkeypatch,
):
    emitted = []
    backend._pending_libris_intake = {
        'prompt': 'Compare graph schedulers',
        'goal_options': ['Favor novelty'],
        'selected_goal': '',
        'stop_condition': '',
    }
    started = False

    def fake_start(request_id):
        nonlocal started
        started = True

    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(backend, '_start_libris_from_pending', fake_start)

    backend.handle_command('/libris launch 2', 'req-invalid')

    assert started is False
    assert backend._pending_libris_intake['selected_goal'] == ''
    assert emitted == [{
        'type': 'error',
        'error': 'Invalid Libris goal option: 2',
        'request_id': 'req-invalid',
    }]


def test_libris_cancel_clears_pending_intake(backend, monkeypatch):
    emitted = []
    backend._pending_libris_intake = {
        'prompt': 'Compare graph schedulers',
        'goal_options': ['Favor novelty'],
        'selected_goal': '',
        'stop_condition': '',
    }
    monkeypatch.setattr(common, 'emit', emitted.append)

    backend.handle_command('/libris cancel', 'req-cancel')

    assert backend._pending_libris_intake is None
    assert emitted == [{
        'type': 'status',
        'message': 'Cancelled pending Libris intake.',
        'request_id': 'req-cancel',
    }]


def test_libris_legacy_selection_commands_remain_available(
    backend,
    monkeypatch,
):
    emitted = []
    launches = []
    backend._pending_libris_intake = {
        'prompt': 'Compare graph schedulers',
        'goal_options': ['Favor novelty', 'Favor measured reliability'],
        'selected_goal': '',
        'stop_condition': '',
    }
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_start_libris_from_pending',
        lambda request_id: launches.append(request_id),
    )

    backend.handle_command('/libris use 2', 'req-use')
    assert backend._pending_libris_intake['selected_goal'] == (
        'Favor measured reliability'
    )

    backend.handle_command('/libris custom Favor reproducibility', 'req-custom')
    assert backend._pending_libris_intake['selected_goal'] == (
        'Favor reproducibility'
    )

    backend.handle_command('/libris stop after 50000 tokens', 'req-stop')
    assert backend._pending_libris_intake['stop_condition'] == (
        'after 50000 tokens'
    )

    backend.handle_command('/libris go', 'req-go')
    assert launches == ['req-go']


def test_libris_status_without_id_uses_most_recent_and_lists_artifacts(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    backend._last_libris_operation_id = 'rop-ready'
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_load_libris_swarm',
        lambda operation_id: _ready_swarm(tmp_path, operation_id),
    )

    backend.handle_command('/libris status', 'req-status')

    assert len(emitted) == 1
    assert emitted[0]['type'] == 'status'
    message = emitted[0]['message']
    assert 'Libris operation: rop-ready' in message
    assert 'Complete. Final deliverables are ready.' in message
    assert 'Primary artifact (Research report):' in message
    assert str((tmp_path / 'delivery' / 'report.html').resolve()) in message
    assert str((tmp_path / 'delivery' / 'executive-summary.md').resolve()) in message


def test_libris_status_recovers_latest_operation_after_backend_restart(
    backend,
    monkeypatch,
):
    emitted = []
    requested = []
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_latest_libris_operation_id',
        lambda: 'rop-recovered',
    )

    def fake_swarm(operation_id):
        requested.append(operation_id)
        return {
            'operation_id': operation_id,
            'status': 'researching',
            'topics': [],
            'delivery_manifest': {
                'status': 'working',
                'ready': False,
                'topic_count': 0,
                'primary_artifact': {},
                'artifacts': [],
            },
        }

    monkeypatch.setattr(backend, '_load_libris_swarm', fake_swarm)

    backend.handle_command('/libris status', 'req-recovered')

    assert requested == ['rop-recovered']
    assert backend._last_libris_operation_id == 'rop-recovered'
    assert 'Still running' in emitted[0]['message']


def test_libris_approval_policy_command_is_persisted(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    state_dir = tmp_path / 'state'
    monkeypatch.delenv('CHARON_RESEARCH_SOURCE_APPROVAL', raising=False)
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)
    monkeypatch.setattr(common, 'emit', emitted.append)

    backend.handle_command('/libris approvals ask', 'req-approvals')

    assert emitted[0]['type'] == 'status'
    assert 'Effective policy: ask' in emitted[0]['message']
    assert 'dangerous commands remain gated' in emitted[0]['message']
    assert common._load_json(state_dir / 'approval_config.json', {}) == {
        'research_sources': 'ask',
    }


def test_libris_status_explicit_id_overrides_most_recent(
    backend,
    monkeypatch,
):
    emitted = []
    requested = []
    backend._last_libris_operation_id = 'rop-old'
    monkeypatch.setattr(common, 'emit', emitted.append)

    def fake_swarm(operation_id):
        requested.append(operation_id)
        return {
            'operation_id': operation_id,
            'status': 'researching',
            'topics': [],
            'delivery_manifest': {
                'status': 'working',
                'ready': False,
                'topic_count': 0,
                'primary_artifact': {},
                'artifacts': [],
            },
        }

    monkeypatch.setattr(backend, '_load_libris_swarm', fake_swarm)

    backend.handle_command('/libris status rop-new', 'req-explicit')

    assert requested == ['rop-new']
    assert 'Libris operation: rop-new' in emitted[0]['message']


def test_libris_delivered_without_valid_manifest_is_reported_incomplete(
    backend,
    monkeypatch,
):
    emitted = []
    backend._last_libris_operation_id = 'rop-empty'
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_load_libris_swarm',
        lambda operation_id: {
            'operation_id': operation_id,
            'status': 'delivered',
            'topics': [{'title': 'Topic', 'status': 'checkpointed'}],
            'delivery_manifest': {
                'status': 'incomplete',
                'ready': False,
                'topic_count': 0,
                'primary_artifact': {},
                'artifacts': [],
            },
        },
    )

    backend.handle_command('/libris status', 'req-incomplete')

    message = emitted[0]['message']
    assert 'Delivery is incomplete.' in message
    assert 'Do not treat this run as successfully completed.' in message
    assert 'Final deliverables are ready' not in message


@pytest.mark.parametrize(
    'question',
    [
        'did it finish?',
        'is it done?',
        'where are the results?',
        'where should I look for the output?',
    ],
)
def test_libris_followup_questions_bypass_the_model(
    backend,
    monkeypatch,
    tmp_path,
    question,
):
    emitted = []
    backend._last_libris_operation_id = 'rop-ready'
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_load_libris_swarm',
        lambda operation_id: _ready_swarm(tmp_path, operation_id),
    )
    monkeypatch.setattr(
        backend,
        '_ensure_engine',
        lambda: pytest.fail('Libris status questions must not invoke the model'),
    )

    backend.handle_chat(question, 'req-question')

    assert [event['type'] for event in emitted] == [
        'status',
        'chat_complete',
    ]
    assert 'Complete. Final deliverables are ready.' in emitted[0]['message']


def test_explicit_libris_question_without_session_operation_is_intercepted(
    backend,
    monkeypatch,
):
    emitted = []
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(backend, '_latest_libris_operation_id', lambda: '')
    monkeypatch.setattr(
        backend,
        '_ensure_engine',
        lambda: pytest.fail('An explicit Libris question must not invoke the model'),
    )

    backend.handle_chat('is Libris done?', 'req-no-op')

    assert [event['type'] for event in emitted] == [
        'error',
        'chat_complete',
    ]
    assert 'No Libris operation is associated with this session' in emitted[0]['error']


def test_running_libris_status_does_not_claim_an_artifact_is_ready(
    backend,
    monkeypatch,
):
    emitted = []
    backend._last_libris_operation_id = 'rop-running'
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_load_libris_swarm',
        lambda operation_id: {
            'operation_id': operation_id,
            'status': 'judging',
            'topics': [
                {'title': 'A', 'status': 'checkpointed'},
                {'title': 'B', 'status': 'judging'},
            ],
            'delivery_manifest': {
                'status': 'working',
                'ready': False,
                'topic_count': 1,
                'primary_artifact': {},
                'artifacts': [],
            },
        },
    )

    backend.handle_command('/libris status', 'req-running')

    message = emitted[0]['message']
    assert 'Still running — current stage: judging.' in message
    assert 'No final artifact is ready yet.' in message
    assert '1 checkpointed' in message
    assert '1 active' in message


def test_refresh_emits_libris_completed_once_for_a_valid_delivery(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    backend._last_libris_operation_id = 'rop-ready'
    backend._tracked_libris_operation_ids = ['rop-ready']
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        DashboardMixin,
        '_get_refresh_payload',
        lambda self: {'inter_agent_rooms': []},
    )
    monkeypatch.setattr(
        backend,
        '_load_libris_swarm',
        lambda operation_id: _ready_swarm(tmp_path, operation_id),
    )

    assert backend._get_refresh_payload() == {'inter_agent_rooms': []}
    assert backend._get_refresh_payload() == {'inter_agent_rooms': []}

    completion_events = [
        event for event in emitted if event['type'] == 'libris_completed'
    ]
    assert len(completion_events) == 1
    assert completion_events[0]['operation_id'] == 'rop-ready'
    assert completion_events[0]['topic_count'] == 1
    assert completion_events[0]['artifact_count'] == 2
    assert completion_events[0]['primary_artifact']['path'] == str(
        (tmp_path / 'delivery' / 'report.html').resolve()
    )


def test_fresh_backend_does_not_replay_latest_historical_completion(
    backend,
    monkeypatch,
):
    emitted = []
    looked_up_latest = False

    def latest_operation():
        nonlocal looked_up_latest
        looked_up_latest = True
        return 'rop-historical'

    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(backend, '_latest_libris_operation_id', latest_operation)

    backend._maybe_emit_libris_completed()

    assert emitted == []
    assert looked_up_latest is False


def test_refresh_announces_older_tracked_completion_before_newer_run_finishes(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    loaded = []
    backend._last_libris_operation_id = 'rop-newer'
    backend._tracked_libris_operation_ids = ['rop-older', 'rop-newer']
    swarms = {
        'rop-older': _ready_swarm(tmp_path, 'rop-older'),
        'rop-newer': {
            'operation_id': 'rop-newer',
            'status': 'researching',
            'topics': [],
            'delivery_manifest': {
                'status': 'working',
                'ready': False,
                'topic_count': 0,
                'primary_artifact': {},
                'artifacts': [],
            },
        },
    }

    def fake_swarm(operation_id):
        loaded.append(operation_id)
        return swarms[operation_id]

    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(backend, '_load_libris_swarm', fake_swarm)

    backend._maybe_emit_libris_completed()
    assert [event['operation_id'] for event in emitted] == ['rop-older']

    swarms['rop-newer'] = _ready_swarm(tmp_path, 'rop-newer')
    backend._maybe_emit_libris_completed()
    backend._maybe_emit_libris_completed()

    assert [event['operation_id'] for event in emitted] == [
        'rop-older',
        'rop-newer',
    ]
    assert loaded == ['rop-older', 'rop-newer', 'rop-newer']


def test_completion_scan_falls_back_to_latest_for_legacy_backend_session(
    backend,
    monkeypatch,
    tmp_path,
):
    emitted = []
    del backend._tracked_libris_operation_ids
    monkeypatch.setattr(common, 'emit', emitted.append)
    monkeypatch.setattr(
        backend,
        '_latest_libris_operation_id',
        lambda: 'rop-legacy-latest',
    )
    monkeypatch.setattr(
        backend,
        '_load_libris_swarm',
        lambda operation_id: _ready_swarm(tmp_path, operation_id),
    )

    backend._maybe_emit_libris_completed()
    backend._maybe_emit_libris_completed()

    completion_events = [
        event for event in emitted if event['type'] == 'libris_completed'
    ]
    assert [event['operation_id'] for event in completion_events] == [
        'rop-legacy-latest',
    ]
    assert backend._last_libris_operation_id == 'rop-legacy-latest'
