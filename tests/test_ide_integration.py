"""Public tool registration, operator configuration, and approval boundaries."""
import json
from dataclasses import replace

import pytest

from charon.ide.config import configured_context
from charon.infra.tool_approval import classify_tool_risk
from charon.tools import ALL_TOOL_DEFS, TOOL_EXECUTORS, ToolContext, ToolResult, execute_tool
from charon.tools.tool_catalog import CORE_TOOL_NAMES, _matches


def test_ide_tools_registered_and_discoverable():
    names = [tool['name'] for tool in ALL_TOOL_DEFS]
    for name in ('Lsp', 'Ast', 'Debug'):
        assert names.count(name) == 1
        assert callable(TOOL_EXECUTORS[name])
        assert name not in CORE_TOOL_NAMES
        assert name in [item['name'] for item in _matches(ALL_TOOL_DEFS, name)]


@pytest.mark.parametrize('name,action,risk', [
    ('Lsp', 'status', 'safe'), ('Lsp', 'definition', 'dangerous'),
    ('Lsp', 'diagnostics', 'dangerous'), ('Lsp', 'shutdown', 'safe'),
    ('Debug', 'launch', 'dangerous'), ('Debug', 'continue', 'dangerous'),
    ('Debug', 'breakpoints', 'dangerous'), ('Debug', 'variables', 'safe'),
    ('Debug', 'terminate', 'safe'), ('Ast', 'search', 'safe'),
    ('Ast', 'preview', 'safe'), ('Ast', 'apply', 'write'),
])
def test_action_aware_ide_risk(name, action, risk):
    assert classify_tool_risk(name, {'action': action})[0] == risk


def test_config_missing_and_metadata_override(tmp_path):
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state')
    assert configured_context(ctx) is ctx
    ctx.state_dir.mkdir()
    data = {'ide_lsp_servers': [], 'ide_debug_adapters': {}}
    (ctx.state_dir / 'ide.json').write_text(json.dumps(data))
    configured = configured_context(ctx)
    assert configured.metadata == data
    assert ctx.metadata is None
    explicit = replace(ctx, metadata={'ide_lsp_servers': [{'name': 'explicit'}]})
    assert configured_context(explicit).metadata['ide_lsp_servers'] == [{'name': 'explicit'}]


@pytest.mark.parametrize('data', ['{', '[]', '{"ide_lsp_servers": {}}', '{"ide_debug_adapters": []}'])
def test_bad_operator_config_fails_visibly(tmp_path, data):
    (tmp_path / 'ide.json').write_text(data)
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path)
    with pytest.raises(ValueError):
        configured_context(ctx)
    result = execute_tool('Lsp', {'action': 'status'}, ctx)
    assert result.is_error
    assert 'configuration' in result.content or 'must be' in result.content


def test_project_config_cannot_select_executable(tmp_path):
    (tmp_path / 'ide.json').write_text('{"ide_lsp_servers":[{"command":["evil"]}]}')
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path / 'operator-state')
    assert configured_context(ctx) is ctx


def test_config_rejects_symlink(tmp_path):
    other = tmp_path / 'other.json'
    other.write_text('{}')
    (tmp_path / 'ide.json').symlink_to(other)
    with pytest.raises(ValueError, match='non-symlink'):
        configured_context(ToolContext(project_root=tmp_path, state_dir=tmp_path))


@pytest.mark.parametrize('restrictions', [{'scope': ['src']}, {'frozen': ['tests']}])
def test_debug_restrictions_hold_even_with_approval_disabled(tmp_path, monkeypatch, restrictions):
    monkeypatch.setenv('CHARON_SKIP_APPROVAL', '1')
    monkeypatch.setitem(TOOL_EXECUTORS, 'Debug', lambda *_: pytest.fail('dispatched restricted debugger'))
    ctx = ToolContext(project_root=tmp_path, **restrictions)
    result = execute_tool('Debug', {'action': 'launch'}, ctx)
    assert result.is_error and 'Scope violation' in result.content


def test_config_applied_at_public_dispatch(tmp_path, monkeypatch):
    (tmp_path / 'ide.json').write_text('{"ide_lsp_servers":[]}')
    monkeypatch.setitem(TOOL_EXECUTORS, 'Lsp', lambda p, ctx: ToolResult('ok', details=ctx.metadata))
    result = execute_tool('Lsp', {'action': 'status'}, ToolContext(project_root=tmp_path, state_dir=tmp_path))
    assert result.details['ide_lsp_servers'] == []


def test_debug_and_lsp_cannot_launch_on_denied_approval(tmp_path, monkeypatch):
    import charon.tools as tools
    import charon.infra.tool_approval as approval
    monkeypatch.setattr(approval, 'needs_approval', lambda *a, **k: (True, 'dangerous', 'host execution'))
    monkeypatch.setattr(tools, '_request_interactive_approval', lambda *a: (False, 'denied'))
    for name in ('Lsp', 'Debug'):
        monkeypatch.setitem(TOOL_EXECUTORS, name, lambda *_: pytest.fail('executed denied tool'))
        result = execute_tool(name, {'action': 'launch'}, ToolContext(project_root=tmp_path))
        assert result.is_error and 'user denied' in result.content


def test_successful_writes_invalidate_lsp_but_failures_do_not(tmp_path, monkeypatch):
    from charon.ide import lsp
    from charon.tools import execute_write, execute_edit
    seen = []
    monkeypatch.setattr(lsp, 'notify_file_changed', lambda path, ctx: seen.append(path))
    ctx = ToolContext(project_root=tmp_path)
    assert not execute_write({'path': 'a.py', 'content': 'old\n'}, ctx).is_error
    assert not execute_edit({'path': 'a.py', 'oldText': 'old', 'newText': 'new'}, ctx).is_error
    assert execute_edit({'path': 'a.py', 'oldText': 'absent', 'newText': 'no'}, ctx).is_error
    assert seen == [tmp_path / 'a.py', tmp_path / 'a.py']


def test_invalidation_failure_does_not_misreport_disk_write(tmp_path, monkeypatch):
    from charon.ide import lsp
    from charon.tools import execute_write
    def broken(*args):
        raise RuntimeError('dead server')
    monkeypatch.setattr(lsp, 'notify_file_changed', broken)
    result = execute_write({'path': 'a.py', 'content': 'saved'}, ToolContext(project_root=tmp_path))
    assert not result.is_error
    assert (tmp_path / 'a.py').read_text() == 'saved'
