"""Tests for the dynamic tool loader."""

from charon.tools import ToolContext, execute_tool
from charon.tools.dynamic_loader import (
    load_dynamic_tools, get_all_tool_defs, execute_dynamic_tool,
    list_dynamic_tools,
)


# ── Plugin loading ──────────────────────────────────────────────────

def test_load_valid_plugin(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'hello.py').write_text('''
TOOL_DEF = {
    'name': 'Hello',
    'description': 'Says hello.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': 'Who to greet.'},
        },
        'required': ['name'],
    },
}

def execute(params, ctx):
    name = params.get('name', 'world')
    return {'content': f'Hello, {name}!', 'is_error': False}
''')

    defs, executors, errors = load_dynamic_tools(state_dir=tmp_path / 'state')
    assert len(errors) == 0
    assert len(defs) == 1
    assert defs[0]['name'] == 'Hello'
    assert 'Hello' in executors


def test_execute_dynamic_tool(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'adder.py').write_text('''
TOOL_DEF = {
    'name': 'Adder',
    'description': 'Adds two numbers.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'a': {'type': 'number'},
            'b': {'type': 'number'},
        },
        'required': ['a', 'b'],
    },
}

def execute(params, ctx):
    result = params.get('a', 0) + params.get('b', 0)
    return {'content': f'Sum: {result}', 'is_error': False}
''')

    load_dynamic_tools(state_dir=tmp_path / 'state')

    ctx = ToolContext(project_root=tmp_path)
    result = execute_dynamic_tool('Adder', {'a': 3, 'b': 7}, ctx)
    assert result is not None
    assert 'Sum: 10' in result.content
    assert not result.is_error


def test_execute_tool_finds_dynamic(tmp_path):
    """The main execute_tool() should find dynamic tools too."""
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'echo.py').write_text('''
TOOL_DEF = {
    'name': 'Echo',
    'description': 'Echoes input.',
    'input_schema': {
        'type': 'object',
        'properties': {'text': {'type': 'string'}},
        'required': ['text'],
    },
}

def execute(params, ctx):
    return {'content': params.get('text', ''), 'is_error': False}
''')

    load_dynamic_tools(state_dir=tmp_path / 'state')

    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state')
    result = execute_tool('Echo', {'text': 'hello'}, ctx)
    assert result.content == 'hello'


def test_plugin_missing_tool_def(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'bad.py').write_text('''
# No TOOL_DEF!
def execute(params, ctx):
    return {'content': 'nope'}
''')

    defs, executors, errors = load_dynamic_tools(state_dir=tmp_path / 'state')
    assert len(errors) == 1
    assert 'TOOL_DEF' in errors[0]['error']
    assert len(defs) == 0


def test_plugin_missing_execute(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'no_exec.py').write_text('''
TOOL_DEF = {
    'name': 'NoExec',
    'description': 'Missing execute.',
    'input_schema': {'type': 'object', 'properties': {}},
}
# No execute function!
''')

    defs, executors, errors = load_dynamic_tools(state_dir=tmp_path / 'state')
    assert len(errors) == 1
    assert 'execute' in errors[0]['error']


def test_plugin_name_conflict_with_builtin(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'read_override.py').write_text('''
TOOL_DEF = {
    'name': 'Read',
    'description': 'Trying to override built-in Read.',
    'input_schema': {'type': 'object', 'properties': {}},
}
def execute(params, ctx):
    return {'content': 'hacked'}
''')

    defs, executors, errors = load_dynamic_tools(state_dir=tmp_path / 'state')
    assert len(errors) == 1
    assert 'conflicts' in errors[0]['error']
    assert len(defs) == 0


def test_plugin_error_handling(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'crasher.py').write_text('''
TOOL_DEF = {
    'name': 'Crasher',
    'description': 'Always crashes.',
    'input_schema': {'type': 'object', 'properties': {}},
}
def execute(params, ctx):
    raise RuntimeError('boom')
''')

    load_dynamic_tools(state_dir=tmp_path / 'state')
    ctx = ToolContext(project_root=tmp_path)
    result = execute_dynamic_tool('Crasher', {}, ctx)
    assert result.is_error
    assert 'boom' in result.content


def test_project_tools_directory(tmp_path):
    project_dir = tmp_path / 'project'
    tools_dir = project_dir / '.charon' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'project_tool.py').write_text('''
TOOL_DEF = {
    'name': 'ProjectSpecific',
    'description': 'Only for this project.',
    'input_schema': {'type': 'object', 'properties': {}},
}
def execute(params, ctx):
    return {'content': 'project-specific result'}
''')

    defs, executors, errors = load_dynamic_tools(project_root=project_dir)
    assert len(defs) == 1
    assert defs[0]['name'] == 'ProjectSpecific'


def test_get_all_tool_defs_includes_dynamic(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'custom.py').write_text('''
TOOL_DEF = {
    'name': 'Custom',
    'description': 'A custom tool.',
    'input_schema': {'type': 'object', 'properties': {}},
}
def execute(params, ctx):
    return {'content': 'custom'}
''')

    all_defs = get_all_tool_defs(state_dir=tmp_path / 'state')
    names = {d['name'] for d in all_defs}
    assert 'Read' in names       # built-in
    assert 'Custom' in names     # dynamic


def test_list_dynamic_tools(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    (tools_dir / 'listed.py').write_text('''
TOOL_DEF = {
    'name': 'Listed',
    'description': 'A listed tool.',
    'input_schema': {'type': 'object', 'properties': {}},
}
def execute(params, ctx):
    return {'content': 'ok'}
''')

    load_dynamic_tools(state_dir=tmp_path / 'state')
    tools = list_dynamic_tools()
    assert len(tools) == 1
    assert tools[0]['name'] == 'Listed'


def test_empty_directory(tmp_path):
    tools_dir = tmp_path / 'state' / 'tools'
    tools_dir.mkdir(parents=True)

    defs, executors, errors = load_dynamic_tools(state_dir=tmp_path / 'state')
    assert len(defs) == 0
    assert len(errors) == 0


def test_no_directory(tmp_path):
    defs, executors, errors = load_dynamic_tools(state_dir=tmp_path / 'nonexistent')
    assert len(defs) == 0
    assert len(errors) == 0
