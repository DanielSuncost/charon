"""Vestigial re-export shim for project_registry.ensure_project.

Historically this module loaded project_registry by file path so that
importlib-loaded, path-launched modules could resolve it without
the daemon directory on sys.path. Now that everything lives in the installable
`charon` package, callers can simply `from charon.infra.project_registry
import ensure_project`; this shim only keeps the old indirection points
(`load_ensure_project` / `load_ensure_project_from_tools`) working.
"""
from __future__ import annotations

from typing import Callable

from charon.infra.project_registry import ensure_project


def load_ensure_project(caller_file: str, module_tag: str = 'runtime') -> Callable:
    """Vestigial: returns charon.infra.project_registry.ensure_project."""
    return ensure_project


def load_ensure_project_from_tools(caller_file: str, module_tag: str = 'tools') -> Callable:
    """Vestigial: returns charon.infra.project_registry.ensure_project."""
    return ensure_project
