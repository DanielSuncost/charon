"""Tests for checkpoint_manager.py — shadow git snapshots."""
import pytest

from charon.automation.checkpoint_manager import CheckpointManager


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace with some files."""
    work_dir = tmp_path / 'project'
    work_dir.mkdir()
    (work_dir / 'main.py').write_text('print("hello")\n')
    (work_dir / 'config.yaml').write_text('debug: true\n')
    sub = work_dir / 'src'
    sub.mkdir()
    (sub / 'lib.py').write_text('def add(a, b): return a + b\n')

    state_dir = tmp_path / 'state'
    state_dir.mkdir()

    return work_dir, state_dir


@pytest.fixture
def mgr(workspace):
    work_dir, state_dir = workspace
    return CheckpointManager(state_dir, work_dir)


class TestSnapshot:
    def test_creates_checkpoint(self, mgr, workspace):
        work_dir, _ = workspace
        sha = mgr.snapshot('initial')
        assert sha
        assert len(sha) == 40  # full git sha

    def test_multiple_snapshots(self, mgr, workspace):
        work_dir, _ = workspace
        sha1 = mgr.snapshot('first')
        (work_dir / 'main.py').write_text('print("modified")\n')
        sha2 = mgr.snapshot('second')
        assert sha1 != sha2

    def test_empty_snapshot(self, mgr, workspace):
        """Snapshot with no changes still creates a commit."""
        sha1 = mgr.snapshot('first')
        sha2 = mgr.snapshot('second-no-changes')
        # Both should succeed (--allow-empty)
        assert sha1
        assert sha2


class TestRollback:
    def test_rollback_restores_file(self, mgr, workspace):
        work_dir, _ = workspace
        original = (work_dir / 'main.py').read_text()
        sha = mgr.snapshot('before-change')

        (work_dir / 'main.py').write_text('print("broken")\n')
        assert (work_dir / 'main.py').read_text() == 'print("broken")\n'

        result = mgr.rollback(sha)
        assert result is True
        assert (work_dir / 'main.py').read_text() == original

    def test_rollback_restores_deleted_file(self, mgr, workspace):
        work_dir, _ = workspace
        sha = mgr.snapshot('before-delete')

        (work_dir / 'config.yaml').unlink()
        assert not (work_dir / 'config.yaml').exists()

        mgr.rollback(sha)
        assert (work_dir / 'config.yaml').exists()
        assert (work_dir / 'config.yaml').read_text() == 'debug: true\n'

    def test_rollback_invalid_sha(self, mgr):
        result = mgr.rollback('0000000000000000000000000000000000000000')
        assert result is False


class TestScope:
    def test_scoped_snapshot(self, workspace):
        work_dir, state_dir = workspace
        mgr = CheckpointManager(state_dir, work_dir, scope=['main.py'])
        sha = mgr.snapshot('scoped')
        assert sha

    def test_scoped_dir(self, workspace):
        work_dir, state_dir = workspace
        mgr = CheckpointManager(state_dir, work_dir, scope=['src/'])
        sha = mgr.snapshot('scoped-dir')
        assert sha


class TestListCheckpoints:
    def test_list_after_snapshots(self, mgr, workspace):
        work_dir, _ = workspace
        mgr.snapshot('alpha')
        (work_dir / 'main.py').write_text('v2\n')
        mgr.snapshot('beta')

        cps = mgr.list_checkpoints()
        labels = [cp.label for cp in cps]
        assert 'alpha' in labels
        assert 'beta' in labels

    def test_list_empty(self, mgr):
        cps = mgr.list_checkpoints()
        # Only the init commit exists, which is filtered
        assert len(cps) == 0


class TestDiff:
    def test_diff_shows_changes(self, mgr, workspace):
        work_dir, _ = workspace
        sha = mgr.snapshot('baseline')
        (work_dir / 'main.py').write_text('print("changed")\n')
        diff = mgr.diff(sha)
        assert 'main.py' in diff

    def test_diff_no_changes(self, mgr, workspace):
        sha = mgr.snapshot('baseline')
        diff = mgr.diff(sha)
        # No changes — diff should be empty or minimal
        assert 'main.py' not in diff


class TestExists:
    def test_valid_checkpoint(self, mgr):
        sha = mgr.snapshot('test')
        assert mgr.exists(sha)

    def test_invalid_checkpoint(self, mgr):
        mgr.snapshot('ensure-init')
        assert not mgr.exists('deadbeef' * 5)


class TestChangedPathsUnder:
    """The frozen-path detector must see changes by any means without
    corrupting the persistent index."""

    def test_detects_modified_frozen_file(self, mgr, workspace):
        work_dir, _ = workspace
        base = mgr.snapshot('baseline')
        assert mgr.changed_paths_under(base, ['config.yaml']) == []
        (work_dir / 'config.yaml').write_text('debug: false\n')
        assert 'config.yaml' in mgr.changed_paths_under(base, ['config.yaml'])

    def test_detects_newly_created_file_under_frozen_dir(self, mgr, workspace):
        work_dir, _ = workspace
        base = mgr.snapshot('baseline')
        (work_dir / 'src' / 'secret.py').write_text('KEY = 1\n')
        changed = mgr.changed_paths_under(base, ['src/'])
        assert any('secret.py' in p for p in changed)

    def test_detection_does_not_pollute_index(self, mgr, workspace):
        """Regression for the frozen-file-deletion bug: changed_paths_under must
        not leave files staged in the PERSISTENT index. Previously it ran a bare
        `git add -A`, so a subsequent snapshot committed whatever it staged, and a
        later rollback to an earlier checkpoint deleted those files. The detector
        now stages into a throwaway index instead."""
        work_dir, _ = workspace
        base = mgr.snapshot('baseline')

        # An untracked file appears, then the frozen detector runs.
        (work_dir / 'extra.txt').write_text('side data\n')
        mgr.changed_paths_under(base, ['config.yaml'])

        # The persistent index must NOT have extra.txt staged. (Before the fix,
        # the detector's `git add -A` staged it; a later scope-limited snapshot
        # would then commit it, and a rollback to an earlier checkpoint that
        # never had it would delete it — the frozen-file-deletion bug.)
        staged = mgr._git('diff', '--cached', '--name-only', check=False).stdout
        assert 'extra.txt' not in staged, 'detector polluted the persistent index'
        # Detection must also be repeatable and side-effect-free.
        assert mgr.changed_paths_under(base, ['config.yaml']) == []
