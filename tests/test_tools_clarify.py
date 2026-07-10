

from charon import tools as tools_mod

from charon.tools import clarify_tool as cl_mod


def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_clarify_ask_list_answer(tmp_path):
    ctx = _ctx(tmp_path)
    ask = cl_mod.execute_clarify({'action': 'ask', 'question': 'pick one', 'choices': ['a', 'b']}, ctx)
    assert not ask.is_error
    cid = ask.details['clarification_id']
    lst = cl_mod.execute_clarify({'action': 'list'}, ctx)
    assert cid in lst.content
    ans = cl_mod.execute_clarify({'action': 'answer', 'clarification_id': cid, 'answer': 'a'}, ctx)
    assert not ans.is_error


def test_clarify_answer_applies_worker_provider_choice(tmp_path):
    ctx = _ctx(tmp_path)
    ask = cl_mod.execute_clarify({
        'action': 'ask',
        'question': 'No usable provider is configured for shades. Available providers: codex, lmstudio. Which provider should I use for worker tasks?',
        'choices': ['codex', 'lmstudio'],
    }, ctx)
    cid = ask.details['clarification_id']

    ans = cl_mod.execute_clarify({'action': 'answer', 'clarification_id': cid, 'answer': 'lmstudio'}, ctx)
    assert not ans.is_error
    assert ans.details['applied_result']['provider'] == 'lmstudio'

    import json
    reg = json.loads((ctx.state_dir / 'model_registry.json').read_text())
    assert reg['shade_provider'] == 'lmstudio'
    assert reg['shade_model'] == 'qwen3-30b-a3b'
