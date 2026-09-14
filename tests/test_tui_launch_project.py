import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps/tui/opentui')]


@pytest.mark.parametrize('override,expected', [('', '/configured/project'), (' /overseer/workspace ', '/overseer/workspace')])
def test_launch_project_reaches_prompt_and_engine(monkeypatch, override, expected):
    # imported here: the sys.path line above has to run first
    from backend import providers_mixin as module
    from backend.providers_mixin import ProvidersMixin

    monkeypatch.setenv('CHARON_PROJECT_ROOT', override)
    monkeypatch.delenv('CHARON_AGENT', raising=False)
    monkeypatch.delenv('CHARON_RESUME', raising=False)
    obj = ProvidersMixin()
    obj.engine = None
    obj._active_agent_id = 'test-launch'
    with patch.object(module.common, '_load_json', return_value={'project': '/configured/project'}), \
         patch.object(module, 'create_provider_and_model', return_value=(object(), 'fake', True)), \
         patch.object(module, 'ConversationEngine', side_effect=lambda **kwargs: SimpleNamespace(**kwargs)), \
         patch('charon.context.system_prompt_builder.build_system_prompt', return_value='test role') as prompt:
        engine, error = obj._ensure_engine()
        assert not error
        assert engine.project_root == expected
        assert prompt.call_args.kwargs['agent']['project'] == expected
        assert prompt.call_args.kwargs['task']['project'] == expected
