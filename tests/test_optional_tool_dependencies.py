from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def _probe_optional_tools(mode: str) -> subprocess.CompletedProcess[str]:
    probe = textwrap.dedent(
        """
        import importlib.machinery
        import importlib.util
        from pathlib import Path
        import sys

        root = Path(sys.argv[1])
        mode = sys.argv[2]
        sys.path.insert(0, str(root / 'src'))

        optional = {'playwright', 'sentence_transformers', 'sqlite_vec'}
        real_find_spec = importlib.util.find_spec

        def controlled_find_spec(fullname, package=None):
            if fullname in optional:
                if mode == 'absent':
                    return None
                return importlib.machinery.ModuleSpec(fullname, loader=None)
            return real_find_spec(fullname, package)

        importlib.util.find_spec = controlled_find_spec

        import charon.tools as tools

        names = {tool['name'] for tool in tools.ALL_TOOL_DEFS}
        if mode == 'absent':
            assert 'Browser' not in names
            assert 'Browser' not in tools.TOOL_EXECUTORS
            assert 'Recall' not in names
            assert 'Timeline' not in names

            from charon.tools import browser_tool

            result = browser_tool.execute_browser(
                {'action': 'get_state'},
                tools.ToolContext(project_root=root),
            )
            assert result.is_error
            assert "pip install 'charon[browser]'" in result.content
            assert 'playwright install chromium' in result.content
        else:
            assert 'Browser' in names
            assert 'Browser' in tools.TOOL_EXECUTORS
            assert 'Recall' in names
            assert 'Timeline' in names

            result = tools.TOOL_EXECUTORS['Browser'](
                {'action': 'navigate'},
                tools.ToolContext(project_root=root),
            )
            assert result.is_error
            assert result.content == 'Error: url is required.'
        """
    )
    return subprocess.run(
        [sys.executable, '-c', probe, str(ROOT), mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_optional_tools_are_hidden_when_dependencies_are_absent():
    result = _probe_optional_tools('absent')
    assert result.returncode == 0, result.stderr


def test_optional_tools_remain_registered_when_dependencies_are_present():
    result = _probe_optional_tools('present')
    assert result.returncode == 0, result.stderr
