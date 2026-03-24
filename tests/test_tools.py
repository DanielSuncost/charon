"""Tests for Charon agent tools."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from tools import (
    execute_read, execute_write, execute_edit, execute_bash,
    execute_tool, ToolContext, ToolResult, truncate_output,
    ALL_TOOL_DEFS, TOOL_EXECUTORS,
)


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(project_root=tmp_path)


class TestTruncation:
    def test_no_truncation_needed(self):
        text, trunc = truncate_output('hello\nworld\n', max_lines=100, max_bytes=1000)
        assert text == 'hello\nworld\n'
        assert trunc is False

    def test_line_truncation(self):
        lines = '\n'.join(f'line {i}' for i in range(100))
        text, trunc = truncate_output(lines, max_lines=10, max_bytes=100000)
        assert trunc is True
        assert text.count('\n') <= 10

    def test_byte_truncation(self):
        big = 'x' * 200
        text, trunc = truncate_output(big, max_lines=1000, max_bytes=50)
        assert trunc is True
        assert len(text.encode()) <= 50


class TestReadTool:
    def test_read_file(self, tmp_path):
        (tmp_path / 'hello.txt').write_text('Hello World\nLine 2\n')
        result = execute_read({'path': 'hello.txt'}, _ctx(tmp_path))
        assert not result.is_error
        assert 'Hello World' in result.content

    def test_read_nonexistent(self, tmp_path):
        result = execute_read({'path': 'nope.txt'}, _ctx(tmp_path))
        assert result.is_error
        assert 'not found' in result.content

    def test_read_with_offset(self, tmp_path):
        lines = '\n'.join(f'line {i}' for i in range(20))
        (tmp_path / 'lines.txt').write_text(lines)
        result = execute_read({'path': 'lines.txt', 'offset': 10, 'limit': 5}, _ctx(tmp_path))
        assert not result.is_error
        assert 'line 9' in result.content  # 0-indexed line 9 = 10th line
        assert 'line 14' not in result.content  # should stop after 5 lines

    def test_read_empty_path(self, tmp_path):
        result = execute_read({'path': ''}, _ctx(tmp_path))
        assert result.is_error

    def test_read_directory(self, tmp_path):
        result = execute_read({'path': str(tmp_path)}, _ctx(tmp_path))
        assert result.is_error
        assert 'not a file' in result.content

    def test_read_absolute_path(self, tmp_path):
        f = tmp_path / 'abs.txt'
        f.write_text('absolute content')
        result = execute_read({'path': str(f)}, _ctx(tmp_path))
        assert not result.is_error
        assert 'absolute content' in result.content


class TestWriteTool:
    def test_write_new_file(self, tmp_path):
        result = execute_write({'path': 'new.txt', 'content': 'hello'}, _ctx(tmp_path))
        assert not result.is_error
        assert (tmp_path / 'new.txt').read_text() == 'hello'
        assert 'Successfully wrote' in result.content

    def test_write_creates_directories(self, tmp_path):
        result = execute_write({'path': 'a/b/c.txt', 'content': 'deep'}, _ctx(tmp_path))
        assert not result.is_error
        assert (tmp_path / 'a' / 'b' / 'c.txt').read_text() == 'deep'

    def test_write_overwrite(self, tmp_path):
        (tmp_path / 'exist.txt').write_text('old')
        result = execute_write({'path': 'exist.txt', 'content': 'new'}, _ctx(tmp_path))
        assert not result.is_error
        assert (tmp_path / 'exist.txt').read_text() == 'new'

    def test_write_empty_path(self, tmp_path):
        result = execute_write({'path': '', 'content': 'x'}, _ctx(tmp_path))
        assert result.is_error


class TestEditTool:
    def test_edit_simple(self, tmp_path):
        (tmp_path / 'file.py').write_text('def hello():\n    return "world"\n')
        result = execute_edit({
            'path': 'file.py',
            'oldText': 'return "world"',
            'newText': 'return "charon"',
        }, _ctx(tmp_path))
        assert not result.is_error
        assert 'return "charon"' in (tmp_path / 'file.py').read_text()

    def test_edit_not_found(self, tmp_path):
        (tmp_path / 'file.py').write_text('hello')
        result = execute_edit({
            'path': 'file.py',
            'oldText': 'goodbye',
            'newText': 'hi',
        }, _ctx(tmp_path))
        assert result.is_error
        assert 'not found' in result.content

    def test_edit_multiple_matches(self, tmp_path):
        (tmp_path / 'file.py').write_text('aaa\naaa\n')
        result = execute_edit({
            'path': 'file.py',
            'oldText': 'aaa',
            'newText': 'bbb',
        }, _ctx(tmp_path))
        assert result.is_error
        assert '2 times' in result.content

    def test_edit_file_not_found(self, tmp_path):
        result = execute_edit({
            'path': 'nope.py',
            'oldText': 'x',
            'newText': 'y',
        }, _ctx(tmp_path))
        assert result.is_error

    def test_edit_preserves_rest(self, tmp_path):
        original = 'line1\nline2\nline3\n'
        (tmp_path / 'f.txt').write_text(original)
        execute_edit({
            'path': 'f.txt',
            'oldText': 'line2',
            'newText': 'LINE_TWO',
        }, _ctx(tmp_path))
        content = (tmp_path / 'f.txt').read_text()
        assert 'line1' in content
        assert 'LINE_TWO' in content
        assert 'line3' in content


class TestBashTool:
    def test_echo(self, tmp_path):
        result = execute_bash({'command': 'echo hello'}, _ctx(tmp_path))
        assert not result.is_error
        assert 'hello' in result.content

    def test_exit_code(self, tmp_path):
        result = execute_bash({'command': 'exit 1'}, _ctx(tmp_path))
        assert result.is_error

    def test_timeout(self, tmp_path):
        ctx = ToolContext(project_root=tmp_path, shell_timeout=1)
        result = execute_bash({'command': 'sleep 10'}, ctx)
        assert result.is_error
        assert 'timed out' in result.content

    def test_cwd(self, tmp_path):
        result = execute_bash({'command': 'pwd'}, _ctx(tmp_path))
        assert not result.is_error
        assert str(tmp_path) in result.content

    def test_empty_command(self, tmp_path):
        result = execute_bash({'command': ''}, _ctx(tmp_path))
        assert result.is_error

    def test_stderr_captured(self, tmp_path):
        result = execute_bash({'command': 'echo err >&2; exit 0'}, _ctx(tmp_path))
        assert not result.is_error
        assert 'err' in result.content


class TestToolRegistry:
    def test_all_tools_defined(self):
        names = {t['name'] for t in ALL_TOOL_DEFS}
        # Built-in tools (dynamic tools loaded separately)
        assert {'Read', 'Bash', 'Edit', 'Write', 'UserModel', 'ProjectKnowledge', 'Http', 'Git'}.issubset(names)

    def test_all_executors_registered(self):
        for tool_def in ALL_TOOL_DEFS:
            assert tool_def['name'] in TOOL_EXECUTORS

    def test_execute_tool_unknown(self, tmp_path):
        result = execute_tool('NotReal', {}, _ctx(tmp_path))
        assert result.is_error
        assert 'Unknown tool' in result.content

    def test_execute_tool_by_name(self, tmp_path):
        (tmp_path / 'test.txt').write_text('content')
        result = execute_tool('Read', {'path': 'test.txt'}, _ctx(tmp_path))
        assert not result.is_error
        assert 'content' in result.content

    def test_tool_defs_have_schemas(self):
        for tool_def in ALL_TOOL_DEFS:
            assert 'name' in tool_def
            assert 'description' in tool_def
            assert 'input_schema' in tool_def
            schema = tool_def['input_schema']
            assert schema.get('type') == 'object'
            assert 'properties' in schema
            assert 'required' in schema
