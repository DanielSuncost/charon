from charon.providers import ModelInfo
from charon.context.context_transfer import (
    resolve_transfer_profile,
    compile_transfer_bundle,
    apply_transfer_to_engine,
    estimate_transfer_budget,
)
from charon.memory.execution_memory import record_tool_event, get_last_validation_event


class DummyEngine:
    def __init__(self, provider_name: str, model_id: str, context_window: int = 200000):
        self.provider_name = provider_name
        self.model = ModelInfo(provider=provider_name, model_id=model_id, context_window=context_window)
        self.system_prompt = 'Base system prompt'
        self.messages = []


def _bundle(with_validation: bool = True) -> dict:
    workspace = {
        'project_root': '/tmp/project',
        'git': {
            'branch': 'main',
            'head': 'abc123def456',
            'status': ' M src/charon/context/context_transfer.py',
            'diff_stat': ' src/charon/context/context_transfer.py | 42 +++++++++++++',
        },
        'files_touched': [
            'src/charon/context/context_transfer.py',
            'src/charon/memory/execution_memory.py',
        ],
        'last_validation': {
            'command': 'python -m py_compile src/charon/context/context_transfer.py',
            'tool': 'Bash',
            'status': 'passed',
            'summary': 'py_compile completed successfully',
            'kind': 'build',
        } if with_validation else {},
    }
    return {
        'id': 'xfer-test-1',
        'created_at': '2026-03-27T00:00:00Z',
        'source': {'provider': 'codex', 'session_id': 'sess-1', 'agent_id': 'agent-1'},
        'target': {'provider': 'claude-code'},
        'task': {
            'objective': 'Implement provider switch context transfer.',
            'status': 'in_progress',
            'latest_user': 'please continue the transfer work',
            'latest_assistant': 'I updated context transfer and need to validate it.',
            'next_step': 'Run validation and then tighten transfer tier logic.',
        },
        'state': {
            'decisions': ['Provider switches should prompt continue vs fresh.'],
            'working_memory_summary': 'Transfer should preserve task continuity across providers.',
            'working_memory_notes': ['Recent focus: adaptive transfer tiering'],
        },
        'workspace': workspace,
        'history': {
            'normalized_messages': [
                {'role': 'user', 'content': 'please continue the transfer work', 'timestamp': 1},
                {'role': 'assistant', 'content': 'I updated context transfer and need to validate it.', 'timestamp': 2},
                {'role': 'tool_result', 'content': 'ok', 'tool_name': 'Bash', 'tool_call_id': 'tc-1', 'is_error': False, 'timestamp': 3},
            ],
            'full_transcript_path': '/tmp/project/.charon_state/transfers/xfer-test-1-full-messages.json',
            'full_message_count': 37,
        },
        'execution': {
            'recent_tool_events': [
                {'summary': 'Edited file src/charon/context/context_transfer.py'},
                {'summary': 'Ran command: `python -m py_compile src/charon/context/context_transfer.py` → success'},
            ],
            'relevant_execution_memories': [
                {'content': 'Task episode: adaptive transfer planning and provider-aware replay', 'category': 'task_episode'},
            ],
            'recent_task_episodes': [
                {'objective': 'Improve transfer planning', 'summary': 'Added provider-aware transfer compilation.'},
            ],
        },
        'memory': {
            'project_knowledge': 'Charon is a local coding agent with provider switching and transfer support.',
        },
        'fidelity': {
            'normalized_history': True,
            'execution_events': True,
            'semantic_execution_recall': True,
            'git_snapshot': True,
        },
    }


def test_resolve_transfer_profile_prefers_summary_first_for_local_small_models():
    profile = resolve_transfer_profile('local', 'qwen3-30b-a3b')
    assert profile['preferred_style'] == 'summary_first'
    assert profile['supports_history_replay'] is False
    assert profile['message_mode'] == 'none'
    assert profile['max_context_tokens'] == 65536


def test_compile_transfer_bundle_preserves_core_fields_for_small_budget():
    bundle = _bundle(with_validation=True)
    profile = resolve_transfer_profile('local', 'qwen3-30b-a3b')
    compiled = compile_transfer_bundle(bundle, profile, budget_tokens=5000)

    assert compiled['tier'] == 'minimal'
    assert compiled['sections']['objective']
    assert compiled['sections']['next_step']
    assert 'src/charon/context/context_transfer.py' in compiled['sections']['files_touched']
    assert 'py_compile' in compiled['sections']['validation']
    assert compiled['restore_messages'] == []


def test_compile_transfer_bundle_rich_for_large_budget_replays_messages():
    bundle = _bundle(with_validation=True)
    profile = resolve_transfer_profile('claude-code', 'claude-sonnet-4-20250514')
    compiled = compile_transfer_bundle(bundle, profile, budget_tokens=60000)

    assert compiled['tier'] == 'rich'
    assert compiled['replayed_messages'] >= 2
    assert compiled['strategy']['message_mode'] == 'assistant_user_only'
    roles = [m['role'] for m in compiled['restore_messages']]
    assert 'user' in roles
    assert 'assistant' in roles
    assert 'tool_result' not in roles


def test_apply_transfer_to_engine_sets_compiled_metadata_and_respects_profile():
    bundle = _bundle(with_validation=True)

    local_engine = DummyEngine('local', 'qwen3-30b-a3b', context_window=65536)
    apply_transfer_to_engine(local_engine, bundle)
    assert getattr(local_engine, 'transfer_bundle', None) is bundle
    assert local_engine.transfer_compiled['tier'] in ('compressed', 'minimal')
    assert local_engine.transfer_compiled['replayed_messages'] == 0
    assert 'CONTEXT TRANSFER' in local_engine.system_prompt

    claude_engine = DummyEngine('anthropic', 'claude-sonnet-4-20250514', context_window=200000)
    apply_transfer_to_engine(claude_engine, bundle)
    assert claude_engine.transfer_compiled['tier'] in ('rich', 'standard')
    assert claude_engine.transfer_compiled['replayed_messages'] >= 2
    assert len(claude_engine.messages) >= 2


def test_estimate_transfer_budget_uses_engine_context_window():
    profile = resolve_transfer_profile('claude-code', 'claude-sonnet-4-20250514')
    engine = DummyEngine('anthropic', 'claude-sonnet-4-20250514', context_window=200000)
    budget = estimate_transfer_budget(profile, engine)
    assert budget > 10000
    assert budget < 200000


def test_get_last_validation_event_detects_recent_validation_command(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    state_dir.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)

    record_tool_event(
        state_dir,
        session_id='sess-1',
        agent_id='agent-1',
        provider='openai',
        tool_name='Bash',
        params={'command': 'rg -n "transfer" src'},
        result_content='src/charon/context/context_transfer.py:1:Context transfer',
        is_error=False,
        project_root=str(project_root),
    )
    record_tool_event(
        state_dir,
        session_id='sess-1',
        agent_id='agent-1',
        provider='openai',
        tool_name='Bash',
        params={'command': 'python -m py_compile src/charon/context/context_transfer.py'},
        result_content='',
        is_error=False,
        project_root=str(project_root),
    )

    validation = get_last_validation_event(state_dir, session_id='sess-1')
    assert validation is not None
    assert validation['status'] == 'passed'
    assert 'py_compile' in validation['command']
    assert validation['kind'] == 'build'
