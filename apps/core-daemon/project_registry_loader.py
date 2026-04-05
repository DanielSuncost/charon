from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable


def load_ensure_project(caller_file: str, module_tag: str = 'runtime') -> Callable:
    """Load project_registry.ensure_project with a file-based fallback.

    This keeps standalone importlib-loaded modules working in tests and script-like
    execution contexts where apps/core-daemon is not installed as a package.
    """
    try:
        from project_registry import ensure_project
        return ensure_project
    except ImportError:
        pr_path = Path(caller_file).resolve().parent / 'project_registry.py'
        spec = importlib.util.spec_from_file_location(f'charon_project_registry_{module_tag}', pr_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod.ensure_project


def load_ensure_project_from_tools(caller_file: str, module_tag: str = 'tools') -> Callable:
    """Variant for modules under apps/core-daemon/tools/."""
    try:
        from project_registry import ensure_project
        return ensure_project
    except ImportError:
        pr_path = Path(caller_file).resolve().parents[1] / 'project_registry.py'
        spec = importlib.util.spec_from_file_location(f'charon_project_registry_{module_tag}', pr_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod.ensure_project
