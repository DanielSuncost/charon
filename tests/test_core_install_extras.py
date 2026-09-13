from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

CORE_DEPENDENCIES = ["httpx>=0.28", "websockets>=15.0"]
OPTIONAL_DEPENDENCIES = {
    "memory": {
        "sentence-transformers>=4.0,<5.0",
        "sqlite-vec>=0.1.6",
    },
    "browser": {"playwright>=1.50"},
    "office": {
        "openpyxl>=3.1",
        "python-docx>=1.1",
        "python-pptx>=1.0",
    },
}
BLOCKED_IMPORTS = {
    "sentence_transformers",
    "sqlite_vec",
    "playwright",
    "openpyxl",
    "docx",
    "pptx",
    "torch",
}


def _project_metadata() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_dependency_groups_keep_core_install_lightweight():
    project = _project_metadata()
    extras = project["optional-dependencies"]

    assert project["dependencies"] == CORE_DEPENDENCIES
    assert set(extras) == {*OPTIONAL_DEPENDENCIES, "all", "dev"}
    for name, requirements in OPTIONAL_DEPENDENCIES.items():
        assert set(extras[name]) == requirements
    assert set(extras["all"]) == set().union(*OPTIONAL_DEPENDENCIES.values())


def test_requirements_file_tracks_full_install():
    requirements = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    expected = set(CORE_DEPENDENCIES).union(*OPTIONAL_DEPENDENCIES.values())
    assert set(requirements) == expected


def test_core_entrypoints_do_not_import_optional_dependencies():
    probe = textwrap.dedent(
        f"""
        import importlib
        from pathlib import Path
        import runpy
        import sys

        root = Path(sys.argv[1])
        sys.path[:0] = [str(root / "src"), str(root)]
        blocked = {BLOCKED_IMPORTS!r}

        class RejectOptionalImports:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition(".")[0] in blocked:
                    raise ImportError(
                        f"optional dependency imported on core path: {{fullname}}"
                    )
                return None

        sys.meta_path.insert(0, RejectOptionalImports())

        importlib.import_module("charon.providers.httpx_codex")
        importlib.import_module("charon.providers.provider_bridge")
        engine_module = importlib.import_module(
            "charon.conversation.conversation_engine"
        )
        assert engine_module.ConversationEngine
        importlib.import_module("charon.tools")

        sys.argv = [str(root / "scripts" / "charon_chat.py"), "--help"]
        try:
            runpy.run_path(str(root / "scripts" / "charon_chat.py"), run_name="__main__")
        except SystemExit as exc:
            if exc.code != 0:
                raise
        else:
            raise AssertionError("charon_chat.py --help did not exit")
        """
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"core import probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
