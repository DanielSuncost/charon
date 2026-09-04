import json

from charon.libris import libris_runtime as lr


def test_project_storage_uses_explicit_state_dir_registry(tmp_path):
    """Regression: Libris used project_root.parent/.charon_state instead of the
    state_dir supplied by its caller, making operations disappear in another
    project registry."""
    project_root = tmp_path / "workspace" / "charon"
    project_root.mkdir(parents=True)
    state_dir = project_root / ".charon_state"
    parent_state = project_root.parent / ".charon_state"

    # A conflicting registry at the old, incorrectly-derived location.
    parent_registry = parent_state / "projects" / "registry.json"
    parent_registry.parent.mkdir(parents=True)
    parent_registry.write_text(json.dumps({
        "version": 1,
        "projects": [{
            "id": "wrong-project-id",
            "root_path": str(project_root),
            "roots": [str(project_root)],
        }],
        "root_map": {str(project_root): "wrong-project-id"},
    }))

    op = lr.init_operation(state_dir, project_root, prompt="p")
    right_registry = json.loads((state_dir / "projects" / "registry.json").read_text())
    right_id = right_registry["root_map"][str(project_root.resolve())]

    assert right_id != "wrong-project-id"
    assert (state_dir / "projects" / right_id / "research" / "operations"
            / op["operation_id"] / "operation.json").exists()
    assert not (state_dir / "projects" / "wrong-project-id" / "research").exists()


def test_project_resolution_preserves_existing_research_history(tmp_path):
    project_root = tmp_path / "charon"
    project_root.mkdir()
    state_dir = project_root / ".charon_state"

    legacy = state_dir / "projects" / "legacy-charon"
    legacy.mkdir(parents=True)
    (legacy / "project.json").write_text(json.dumps({
        "id": "different-doc-id",
        "root_path": str(project_root.resolve()),
        "roots": [str(project_root.resolve())],
    }))
    old_op = legacy / "research" / "operations" / "rop_old"
    old_op.mkdir(parents=True)
    (old_op / "operation.json").write_text("{}")

    assert lr.resolve_project_id(state_dir, project_root) == "legacy-charon"
