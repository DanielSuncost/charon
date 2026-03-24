"""Integration test — full judge loop: create → baseline → iterate → converge."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'apps' / 'core-daemon'))

import pytest
from judge_engine import (
    create_loop, run_baseline, run_iteration, check_convergence,
    save_loop, load_loop, format_status,
    QuantitativeJudge, CorrectnessJudge, create_judge,
)
from checkpoint_manager import CheckpointManager


class TestFullQuantitativeLoop:
    """End-to-end: optimize a script's output with checkpoints."""

    def test_converge_on_target(self, tmp_path):
        """Simulate an optimization loop that converges in 3 iterations."""
        work_dir = tmp_path / 'project'
        work_dir.mkdir()
        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        # Create a file that outputs a score
        (work_dir / 'score.txt').write_text('100\n')

        cp_mgr = CheckpointManager(state_dir, work_dir)

        config = create_loop(
            state_dir,
            goal='maximize score',
            project=str(work_dir),
            agent_id='AG-001',
            judge_type='quantitative',
            direction='maximize',
            target_score=200.0,
            eval_command='cat score.txt',
            metric_name='score',
            max_iterations=10,
        )
        judge = create_judge(config)

        # Baseline
        config = run_baseline(config, judge, work_dir, checkpoint_mgr=cp_mgr)
        assert config.baseline == 100.0
        assert config.status == 'running'
        save_loop(state_dir, config)

        # Iteration 1: improve to 150
        (work_dir / 'score.txt').write_text('150\n')
        config, it1 = run_iteration(
            config, judge, work_dir,
            change_summary='boosted to 150',
            checkpoint_mgr=cp_mgr,
        )
        assert it1.kept is True
        assert config.best_score == 150.0

        # Check convergence — not yet
        conv = check_convergence(config)
        assert conv is None

        # Iteration 2: regression to 120 — should be rolled back
        (work_dir / 'score.txt').write_text('120\n')
        config, it2 = run_iteration(
            config, judge, work_dir,
            change_summary='regressed to 120',
            checkpoint_mgr=cp_mgr,
        )
        assert it2.kept is False
        assert config.best_score == 150.0
        # File should be rolled back to 150
        assert (work_dir / 'score.txt').read_text().strip() == '150'

        # Iteration 3: hit target at 210
        (work_dir / 'score.txt').write_text('210\n')
        config, it3 = run_iteration(
            config, judge, work_dir,
            change_summary='hit target at 210',
            checkpoint_mgr=cp_mgr,
        )
        assert it3.kept is True
        assert config.best_score == 210.0

        # Check convergence — target met!
        conv = check_convergence(config)
        assert conv is not None
        assert conv.converged is True
        assert conv.reason == 'target_met'

        # Save and verify persistence
        config.convergence = conv
        config.status = 'completed'
        save_loop(state_dir, config)

        loaded = load_loop(state_dir, config.id)
        assert loaded.status == 'completed'
        assert loaded.best_score == 210.0
        assert len(loaded.iterations) == 4  # baseline + 3 iterations
        assert loaded.convergence.converged is True

        # Format status for display
        status = format_status(loaded)
        assert '210' in status
        assert 'optimize' in status.lower() or 'maximize' in status.lower() or 'score' in status.lower()

    def test_minimize_loop(self, tmp_path):
        """Minimize direction works correctly."""
        work_dir = tmp_path / 'project'
        work_dir.mkdir()
        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        (work_dir / 'latency.txt').write_text('100\n')

        config = create_loop(
            state_dir,
            goal='minimize latency',
            project=str(work_dir),
            agent_id='AG-001',
            direction='minimize',
            target_score=50.0,
            eval_command='cat latency.txt',
            max_iterations=10,
        )
        judge = create_judge(config)

        config = run_baseline(config, judge, work_dir)
        assert config.baseline == 100.0

        # Improvement: 100 → 70
        (work_dir / 'latency.txt').write_text('70\n')
        config, it = run_iteration(config, judge, work_dir, change_summary='reduced')
        assert it.kept is True
        assert config.best_score == 70.0

        # Regression: 70 → 80 (worse for minimize)
        (work_dir / 'latency.txt').write_text('80\n')
        config, it = run_iteration(config, judge, work_dir, change_summary='got worse')
        assert it.kept is False
        assert config.best_score == 70.0

    def test_budget_exhaustion(self, tmp_path):
        """Loop stops when budget is exhausted."""
        work_dir = tmp_path / 'project'
        work_dir.mkdir()
        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        (work_dir / 'score.txt').write_text('50\n')

        config = create_loop(
            state_dir,
            goal='test budget',
            project=str(work_dir),
            agent_id='AG-001',
            eval_command='cat score.txt',
            max_iterations=3,
            target_score=1000.0,  # unreachable
        )
        judge = create_judge(config)
        config = run_baseline(config, judge, work_dir)

        for i in range(3):
            (work_dir / 'score.txt').write_text(f'{50 + i}\n')
            config, _ = run_iteration(config, judge, work_dir, change_summary=f'iter {i+1}')

        conv = check_convergence(config)
        assert conv is not None
        assert conv.reason == 'budget_exhausted'
        assert not conv.converged


class TestCorrectnessLoop:
    """Test pass rate judge."""

    def test_correctness_scoring(self, tmp_path):
        work_dir = tmp_path / 'project'
        work_dir.mkdir()
        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        config = create_loop(
            state_dir,
            goal='fix tests',
            project=str(work_dir),
            agent_id='AG-001',
            judge_type='correctness',
            direction='maximize',
            target_score=1.0,
            eval_command='echo "8 passed, 2 failed"',
            max_iterations=10,
        )
        judge = create_judge(config)
        config = run_baseline(config, judge, work_dir)
        assert config.baseline == pytest.approx(0.8)

        # Improve to all passing
        config.eval_command = 'echo "10 passed"'
        judge2 = create_judge(config)
        config, it = run_iteration(config, judge2, work_dir, change_summary='fixed 2 tests')
        assert it.kept is True
        assert config.best_score == pytest.approx(1.0)

        conv = check_convergence(config)
        assert conv.converged is True
        assert conv.reason == 'target_met'


class TestConstraintEnforcement:
    """Constraint commands must pass before scoring."""

    def test_constraint_blocks_scoring(self, tmp_path):
        work_dir = tmp_path / 'project'
        work_dir.mkdir()
        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        (work_dir / 'score.txt').write_text('100\n')

        cp_mgr = CheckpointManager(state_dir, work_dir)

        config = create_loop(
            state_dir,
            goal='test constraints',
            project=str(work_dir),
            agent_id='AG-001',
            eval_command='cat score.txt',
            constraint_commands=['true'],  # passes initially
            max_iterations=10,
        )
        judge = create_judge(config)
        config = run_baseline(config, judge, work_dir, checkpoint_mgr=cp_mgr)
        assert config.baseline == 100.0

        # Now make constraint fail
        (work_dir / 'score.txt').write_text('999\n')  # would be great score
        config.constraint_commands = ['false']  # but constraint fails

        config, it = run_iteration(
            config, judge, work_dir,
            change_summary='broke constraints',
            checkpoint_mgr=cp_mgr,
        )
        assert it.status == 'constraint_failed'
        assert it.kept is False
        assert config.best_score == 100.0  # unchanged


class TestToolIntegration:
    """Test the SpawnJudgeLoop tool creates loops correctly."""

    def test_tool_create(self, tmp_path):
        from tools import ToolContext
        from tools.judge_loop_tool import execute_judge_loop

        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        ctx = ToolContext(
            project_root=tmp_path,
            agent_id='AG-001',
            state_dir=state_dir,
        )

        result = execute_judge_loop({
            'goal': 'optimize performance',
            'judge_type': 'quantitative',
            'direction': 'minimize',
            'target_score': 50.0,
            'eval_command': 'echo 100',
            'metric_name': 'latency_ms',
            'scope': ['src/'],
            'max_iterations': 15,
            'program': 'Focus on query batching.',
        }, ctx)

        assert not result.is_error
        assert 'created' in result.content.lower()
        assert result.details and result.details.get('loop_id')

        # Verify persistence
        from judge_engine import load_loop
        loop = load_loop(state_dir, result.details['loop_id'])
        assert loop.goal == 'optimize performance'
        assert loop.target_score == 50.0
        assert loop.max_iterations == 15

    def test_tool_status(self, tmp_path):
        from tools import ToolContext
        from tools.judge_loop_tool import execute_judge_loop

        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        ctx = ToolContext(project_root=tmp_path, agent_id='AG-001', state_dir=state_dir)

        # Create
        create_result = execute_judge_loop({
            'goal': 'test status',
            'eval_command': 'echo 42',
        }, ctx)
        loop_id = create_result.details['loop_id']

        # Status
        status_result = execute_judge_loop({
            'goal': '',
            'action': 'status',
            'loop_id': loop_id,
        }, ctx)
        assert not status_result.is_error
        assert 'test status' in status_result.content

    def test_tool_list(self, tmp_path):
        from tools import ToolContext
        from tools.judge_loop_tool import execute_judge_loop

        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        ctx = ToolContext(project_root=tmp_path, agent_id='AG-001', state_dir=state_dir)

        execute_judge_loop({'goal': 'loop1', 'eval_command': 'echo 1'}, ctx)
        execute_judge_loop({'goal': 'loop2', 'eval_command': 'echo 2'}, ctx)

        result = execute_judge_loop({'goal': '', 'action': 'list'}, ctx)
        assert '2 judge loop' in result.content
        assert 'loop1' in result.content
        assert 'loop2' in result.content

    def test_tool_stop(self, tmp_path):
        from tools import ToolContext
        from tools.judge_loop_tool import execute_judge_loop

        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        ctx = ToolContext(project_root=tmp_path, agent_id='AG-001', state_dir=state_dir)

        create_result = execute_judge_loop({'goal': 'stop test', 'eval_command': 'echo 1'}, ctx)
        loop_id = create_result.details['loop_id']

        stop_result = execute_judge_loop({
            'goal': '',
            'action': 'stop',
            'loop_id': loop_id,
        }, ctx)
        assert not stop_result.is_error
        assert 'stopped' in stop_result.content.lower()

        from judge_engine import load_loop
        loop = load_loop(state_dir, loop_id)
        assert loop.status == 'completed'
        assert loop.convergence.reason == 'user_stopped'
