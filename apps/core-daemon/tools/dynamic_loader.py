"""Dynamic tool loader — discovers and loads tools from plugin directories.

Scans two locations for tool plugins:
1. .charon_state/tools/   — global user tools (shared across projects)
2. <project>/.charon/tools/ — project-specific tools

Each plugin is a single .py file with:
- TOOL_DEF: dict with 'name', 'description', 'input_schema'
- execute(params: dict, ctx: ToolContext) -> dict
  Must return {'content': str, 'is_error': bool}

Tools are loaded on engine creation and when explicitly reloaded via /tools reload.
New tools written by the agent are available after reload.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from tools import ToolContext, ToolResult

logger = logging.getLogger(__name__)

# Loaded dynamic tools: {name: (tool_def, executor, source_path)}
_dynamic_tools: dict[str, tuple[dict, Callable, str]] = {}
_load_errors: list[dict] = []


def _wrap_executor(raw_execute: Callable, tool_name: str, source: str) -> Callable:
    """Wrap a plugin's execute() to return ToolResult and handle errors."""
    def wrapped(params: dict, ctx: ToolContext) -> ToolResult:
        try:
            result = raw_execute(params, ctx)
            if isinstance(result, ToolResult):
                return result
            if isinstance(result, dict):
                return ToolResult(
                    content=str(result.get('content', '')),
                    is_error=bool(result.get('is_error', False)),
                    truncated=bool(result.get('truncated', False)),
                )
            return ToolResult(content=str(result))
        except Exception as e:
            return ToolResult(
                content=f'Dynamic tool {tool_name} error: {e}',
                is_error=True,
            )
    return wrapped


def _load_plugin(path: Path) -> tuple[dict | None, Callable | None, str | None]:
    """Load a single plugin file. Returns (tool_def, executor, error)."""
    try:
        mod_name = f'charon_tool_plugin_{path.stem}'
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if not spec or not spec.loader:
            return None, None, f'Cannot load module spec from {path}'

        mod = importlib.util.module_from_spec(spec)
        # Don't pollute sys.modules with plugin modules
        spec.loader.exec_module(mod)

        tool_def = getattr(mod, 'TOOL_DEF', None)
        execute_fn = getattr(mod, 'execute', None)

        if not tool_def or not isinstance(tool_def, dict):
            return None, None, f'{path.name}: missing TOOL_DEF dict'
        if not execute_fn or not callable(execute_fn):
            return None, None, f'{path.name}: missing execute() function'
        if not tool_def.get('name'):
            return None, None, f'{path.name}: TOOL_DEF missing "name" field'
        if not tool_def.get('input_schema'):
            return None, None, f'{path.name}: TOOL_DEF missing "input_schema" field'

        return tool_def, execute_fn, None

    except Exception as e:
        return None, None, f'{path.name}: {e}'


def scan_directories(state_dir: Path | None, project_root: Path | None) -> list[Path]:
    """Find all plugin directories to scan."""
    dirs = []
    if state_dir:
        d = Path(state_dir) / 'tools'
        if d.is_dir():
            dirs.append(d)
    if project_root:
        d = Path(project_root) / '.charon' / 'tools'
        if d.is_dir():
            dirs.append(d)
    return dirs


def load_dynamic_tools(
    state_dir: Path | None = None,
    project_root: Path | None = None,
) -> tuple[list[dict], dict[str, Callable], list[dict]]:
    """Load all dynamic tools from plugin directories.

    Returns:
        (tool_defs, executors, errors)
        - tool_defs: list of tool definition dicts
        - executors: {name: executor_function}
        - errors: list of {path, error} for failed loads
    """
    global _dynamic_tools, _load_errors
    _dynamic_tools.clear()
    _load_errors.clear()

    tool_defs = []
    executors = {}
    errors = []

    dirs = scan_directories(state_dir, project_root)

    for plugin_dir in dirs:
        for py_file in sorted(plugin_dir.glob('*.py')):
            if py_file.name.startswith('_'):
                continue  # skip __init__.py etc

            tool_def, execute_fn, error = _load_plugin(py_file)

            if error:
                err = {'path': str(py_file), 'error': error}
                errors.append(err)
                _load_errors.append(err)
                continue

            name = tool_def['name']

            # Check for name conflicts with built-in tools
            from tools import TOOL_EXECUTORS as builtin_executors
            if name in builtin_executors:
                err = {'path': str(py_file), 'error': f'{name} conflicts with built-in tool'}
                errors.append(err)
                _load_errors.append(err)
                continue

            wrapped = _wrap_executor(execute_fn, name, str(py_file))
            _dynamic_tools[name] = (tool_def, wrapped, str(py_file))
            tool_defs.append(tool_def)
            executors[name] = wrapped

    return tool_defs, executors, errors


def get_all_tool_defs(state_dir: Path | None = None, project_root: Path | None = None) -> list[dict]:
    """Get built-in + dynamic tool definitions."""
    from tools import ALL_TOOL_DEFS

    dynamic_defs, _, _ = load_dynamic_tools(state_dir, project_root)
    return list(ALL_TOOL_DEFS) + dynamic_defs


def get_all_executors(state_dir: Path | None = None, project_root: Path | None = None) -> dict[str, Callable]:
    """Get built-in + dynamic tool executors."""
    from tools import TOOL_EXECUTORS

    _, dynamic_executors, _ = load_dynamic_tools(state_dir, project_root)
    merged = dict(TOOL_EXECUTORS)
    merged.update(dynamic_executors)
    return merged


def execute_dynamic_tool(name: str, params: dict, ctx: ToolContext) -> ToolResult | None:
    """Try to execute a dynamic tool. Returns None if not found."""
    if name in _dynamic_tools:
        _, executor, _ = _dynamic_tools[name]
        return executor(params, ctx)
    return None


def get_load_errors() -> list[dict]:
    """Get errors from the last load."""
    return list(_load_errors)


def list_dynamic_tools() -> list[dict]:
    """List currently loaded dynamic tools."""
    return [
        {'name': name, 'source': source, 'description': td.get('description', '')[:80]}
        for name, (td, _, source) in _dynamic_tools.items()
    ]
