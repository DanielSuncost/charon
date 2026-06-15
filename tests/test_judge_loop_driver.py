"""Tests for the judge-loop driver — the heartbeat-driven controller that
advances loops one step per tick using a pluggable implementer."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'apps' / 'core-daemon'))

from judge_engine import create_loop, load_loop, save_loop
from checkpoint_manager import CheckpointManager
from judge_loop_driver import advance_loop, tick_judge_loops


def _make_implementer(values):
    """An implementer that writes the next value to score.txt each iteration."""
    seq = iter(values)

    def impl(config, working_dir):
        try:
            v = next(seq)
        except StopIteration:
            return None
        (Path(working_dir) / 'score.txt').write_text(f'{v}\n')
        return f'set score to {v}'

    return impl


def _new_loop(state_dir, work, **kw):
    params = dict(
        goal='maximize score',
        project=str(work),
        agent_id='AG-001',
        judge_type='quantitative',
        direction='maximize',
        eval_command='cat score.txt',
        metric_name='score',
        max_iterations=10,
    )
    params.update(kw)
    return create_loop(state_dir, **params)


def test_advance_loop_drives_to_target(tmp_path):
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('100\n')

    config = _new_loop(state, work, target_score=200.0)
    cp = CheckpointManager(state, work)
    impl = _make_implementer([150, 120, 210])  # improve, regress, hit target

    # Tick 1: baseline
    config, ev = advance_loop(state, config, implementer=impl, working_dir=work, checkpoint_mgr=cp)
    assert ev['action'] == 'baseline'
    assert config.baseline == 100.0
    assert config.status == 'running'

    # Tick 2: 150 — improvement, kept
    config, ev = advance_loop(state, config, implementer=impl, working_dir=work, checkpoint_mgr=cp)
    assert ev['action'] == 'iterated' and ev['kept'] is True
    assert config.best_score == 150.0

    # Tick 3: 120 — regression, rolled back to 150
    config, ev = advance_loop(state, config, implementer=impl, working_dir=work, checkpoint_mgr=cp)
    assert ev['kept'] is False
    assert config.best_score == 150.0
    assert (work / 'score.txt').read_text().strip() == '150'

    # Tick 4: 210 — improvement that meets the target → converged
    config, ev = advance_loop(state, config, implementer=impl, working_dir=work, checkpoint_mgr=cp)
    assert ev['kept'] is True
    assert ev.get('converged') is True
    assert ev.get('reason') == 'target_met'
    assert config.status == 'completed'
    assert config.best_score == 210.0

    # Persisted
    loaded = load_loop(state, config.id)
    assert loaded.status == 'completed'
    assert loaded.convergence.converged is True


def test_advance_loop_stops_on_budget(tmp_path):
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('50\n')

    config = _new_loop(state, work, target_score=10_000.0, max_iterations=3)
    impl = _make_implementer([51, 52, 53, 54, 55])

    statuses = []
    for _ in range(8):
        config, ev = advance_loop(state, config, implementer=impl, working_dir=work)
        statuses.append(ev['action'])
        if config.status == 'completed':
            break

    assert config.status == 'completed'
    assert config.convergence.reason == 'budget_exhausted'
    assert config.current_iteration == 3


def test_advance_loop_noop_implementer_counts_failure(tmp_path):
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('100\n')

    config = _new_loop(state, work)
    # baseline
    config, ev = advance_loop(state, config, implementer=lambda c, w: None, working_dir=work)
    assert ev['action'] == 'baseline'
    # implementer returns nothing → failure counted, no iteration recorded
    config, ev = advance_loop(state, config, implementer=lambda c, w: None, working_dir=work)
    assert ev['action'] in ('implement_noop', 'converged')
    assert config.consecutive_failures >= 1


def test_tick_picks_up_created_loop_and_runs_baseline(tmp_path):
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('100\n')

    config = _new_loop(state, work, target_score=200.0)
    assert config.status == 'created'

    impl = _make_implementer([150])
    events = tick_judge_loops(state, implementer=impl, max_loops=1)
    assert len(events) == 1
    assert events[0]['action'] == 'baseline'
    assert events[0]['loop_id'] == config.id

    loaded = load_loop(state, config.id)
    assert loaded.status == 'running'
    assert loaded.baseline == 100.0


def test_rollback_removes_files_added_in_discarded_iteration(tmp_path):
    """A file CREATED during a regressed iteration must not leak into the
    best-known state after rollback."""
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('100\n')

    config = _new_loop(state, work, target_score=10_000.0, max_iterations=5)
    cp = CheckpointManager(state, work)

    steps = iter([
        (150, None),          # iter 1: improvement, kept
        (120, 'junk.py'),     # iter 2: regression + creates a new file
    ])

    def implementer(c, wd):
        try:
            val, addfile = next(steps)
        except StopIteration:
            return None
        (Path(wd) / 'score.txt').write_text(f'{val}\n')
        if addfile:
            (Path(wd) / addfile).write_text('# leaked from a discarded iteration\n')
        return f'set {val}'

    advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)  # baseline
    config, _ = advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)  # iter1 kept
    config, ev = advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)  # iter2 discarded

    assert ev['kept'] is False
    assert config.best_score == 150.0
    # Modified-file rollback (already works): score.txt reverts to the kept 150.
    assert (work / 'score.txt').read_text().strip() == '150'
    # Added-file rollback (the bug): junk.py from the discarded iteration must be gone.
    assert not (work / 'junk.py').exists(), 'rollback leaked a file added during a discarded iteration'


def test_advance_loop_frozen_violation_caught_and_restored(tmp_path):
    """An implementer that edits a frozen file by a means the tool layer can't
    see (a raw write, standing in for a shell command) must be caught by the
    engine-level frozen gate, rolled back, and the frozen file restored — while
    the loop keeps its previously-kept best."""
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('100\n')
    (work / 'locked.py').write_text('V = 1\n')

    config = _new_loop(state, work, target_score=10_000.0, frozen=['locked.py'])
    cp = CheckpointManager(state, work)

    steps = iter([
        ('iter1', 150, None),                 # legit improvement, kept
        ('iter2', 999, 'V = 1\nHACK = 1\n'),  # huge score but edits frozen file
        ('iter3', 175, None),                 # legit improvement again, kept
    ])

    def implementer(c, wd):
        try:
            _, val, frozen_edit = next(steps)
        except StopIteration:
            return None
        (Path(wd) / 'score.txt').write_text(f'{val}\n')
        if frozen_edit:
            (Path(wd) / 'locked.py').write_text(frozen_edit)  # bypasses tool scope
        return f'set {val}'

    advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)  # baseline
    config, ev1 = advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)
    assert ev1['kept'] is True and config.best_score == 150.0

    config, ev2 = advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)
    assert ev2['status'] == 'frozen_violation'
    assert ev2['kept'] is False
    assert config.best_score == 150.0                      # the 999 was not banked
    assert (work / 'locked.py').read_text() == 'V = 1\n'   # frozen file restored
    assert (work / 'score.txt').read_text().strip() == '150'

    # The loop recovers and keeps improving afterwards.
    config, ev3 = advance_loop(state, config, implementer=implementer, working_dir=work, checkpoint_mgr=cp)
    assert ev3['kept'] is True and config.best_score == 175.0


def test_tick_skips_paused_loops(tmp_path):
    work = tmp_path / 'project'; work.mkdir()
    state = tmp_path / 'state'; state.mkdir()
    (work / 'score.txt').write_text('100\n')

    config = _new_loop(state, work)
    config.status = 'paused'
    save_loop(state, config)

    events = tick_judge_loops(state, implementer=_make_implementer([150]), max_loops=5)
    assert events == []  # nothing active to advance
