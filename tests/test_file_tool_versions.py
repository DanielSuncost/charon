from __future__ import annotations

from charon.tools import ToolContext, execute_edit, execute_read


def _ctx(root):
    return ToolContext(project_root=root)


def test_read_version_rejects_stale_edit(tmp_path):
    path = tmp_path / 'example.py'
    path.write_text('value = 1\n', encoding='utf-8')
    read = execute_read({'path': 'example.py'}, _ctx(tmp_path))
    version = read.details['version']
    path.write_text('value = 2\n', encoding='utf-8')

    result = execute_edit({
        'path': 'example.py',
        'baseHash': version,
        'oldText': 'value = 1',
        'newText': 'value = 3',
    }, _ctx(tmp_path))

    assert result.is_error
    assert 'stale edit' in result.content
    assert path.read_text(encoding='utf-8') == 'value = 2\n'


def test_multi_hunk_edit_is_atomic_and_order_independent(tmp_path):
    path = tmp_path / 'example.py'
    path.write_text('alpha = 1\nbeta = 2\ngamma = 3\n', encoding='utf-8')
    version = execute_read({'path': 'example.py'}, _ctx(tmp_path)).details['version']

    result = execute_edit({
        'path': 'example.py',
        'baseHash': version,
        'edits': [
            {'oldText': 'gamma = 3', 'newText': 'gamma = 30'},
            {'oldText': 'alpha = 1', 'newText': 'alpha = 10'},
        ],
    }, _ctx(tmp_path))

    assert not result.is_error
    assert path.read_text(encoding='utf-8') == 'alpha = 10\nbeta = 2\ngamma = 30\n'
    assert result.details['edits'] == 2


def test_invalid_hunk_writes_nothing(tmp_path):
    path = tmp_path / 'example.py'
    original = 'alpha = 1\nbeta = 2\n'
    path.write_text(original, encoding='utf-8')

    result = execute_edit({
        'path': 'example.py',
        'edits': [
            {'oldText': 'alpha = 1', 'newText': 'alpha = 10'},
            {'oldText': 'beta = 20', 'newText': 'beta = 10'},
        ],
    }, _ctx(tmp_path))

    assert result.is_error
    assert path.read_text(encoding='utf-8') == original
    assert 'Closest match' in result.content


def test_read_ranges_and_line_numbers(tmp_path):
    path = tmp_path / 'lines.txt'
    path.write_text(''.join(f'line {i}\n' for i in range(1, 9)), encoding='utf-8')

    result = execute_read({
        'path': 'lines.txt',
        'ranges': [{'start': 2, 'end': 3}, {'start': 7, 'end': 7}],
        'lineNumbers': True,
    }, _ctx(tmp_path))

    assert not result.is_error
    assert '     2\tline 2' in result.content
    assert '     7\tline 7' in result.content
    assert 'line 4' not in result.content


def test_outline_mode_lists_symbols(tmp_path):
    path = tmp_path / 'module.py'
    path.write_text(
        'class Service:\n    def run(self):\n        pass\n\nasync def main():\n    pass\n',
        encoding='utf-8',
    )

    result = execute_read({'path': 'module.py', 'mode': 'outline'}, _ctx(tmp_path))

    assert 'class Service' in result.content
    assert 'def run' in result.content
    assert 'def main' in result.content
