from pathlib import Path
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts' / 'charon_agents.py'

spec_cli = importlib.util.spec_from_file_location('charon_agents_chat_clarify_test', SCRIPT_PATH)
charon_agents = importlib.util.module_from_spec(spec_cli)
sys.modules[spec_cli.name] = charon_agents
spec_cli.loader.exec_module(charon_agents)


def test_chat_clarifications_and_answer_apply_worker_provider(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    clar_path = state / 'clarifications.json'
    clar_path.write_text(json.dumps({
        'items': [{
            'clarification_id': 'clar_test123',
            'question': 'No usable provider is configured for shades. Available providers: codex, lmstudio. Which provider should I use for worker tasks?',
            'choices': ['codex', 'lmstudio'],
            'status': 'pending',
            'asked_by_agent_id': 'AG-1',
            'answer': '',
            'created_at': '2026-01-01T00:00:00+00:00',
            'updated_at': '2026-01-01T00:00:00+00:00',
        }]
    }))

    out = io.StringIO()
    with redirect_stdout(out):
        assert charon_agents._handle_chat_slash_command('/clarifications', agent_id='AG-1', conversation_id='conv-1', session_id='', project='', limit=20)
    rendered = out.getvalue()
    assert 'Pending clarifications:' in rendered
    assert '/clarify clar_test123 codex' in rendered
    assert '/clarify clar_test123 lmstudio' in rendered

    out2 = io.StringIO()
    with redirect_stdout(out2):
        assert charon_agents._handle_chat_slash_command('/clarify clar_test123 lmstudio', agent_id='AG-1', conversation_id='conv-1', session_id='', project='', limit=20)
    rendered2 = out2.getvalue()
    assert 'Clarification answered: clar_test123' in rendered2
    assert 'applied worker provider=lmstudio' in rendered2

    reg = json.loads((state / 'model_registry.json').read_text())
    assert reg['shade_provider'] == 'lmstudio'
