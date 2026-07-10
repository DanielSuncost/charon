"""Tests for judge_engine.py — iterative optimization loops with scoring."""
import pytest

from charon.judge.judge_engine import (
    JudgeLoopConfig, Iteration, QuantitativeJudge, CorrectnessJudge, AestheticJudge, CompositeJudge,
    create_judge, check_convergence, is_improvement,
    build_iteration_prompt, format_status,
    create_loop, run_baseline, run_iteration,
    save_loop, load_loop, list_loops,
    _parse_score, _parse_last_float, _parse_json_field, _parse_pass_rate,
    _parse_llm_verdict,
)


# ── Score parsing ───────────────────────────────────────────────────

class TestScoreParsing:
    def test_last_float_simple(self):
        assert _parse_last_float('score: 142.7') == 142.7

    def test_last_float_multiple(self):
        assert _parse_last_float('step 1: 100.0\nstep 2: 200.5\nfinal: 142.7') == 142.7

    def test_last_float_integer(self):
        assert _parse_last_float('result: 42') == 42.0

    def test_last_float_scientific(self):
        assert _parse_last_float('loss: 1.5e-3') == 1.5e-3

    def test_last_float_negative(self):
        assert _parse_last_float('delta: -3.14') == -3.14

    def test_last_float_empty(self):
        assert _parse_last_float('no numbers here') is None

    def test_json_field(self):
        output = '{"mean_reward": 194.2, "std": 12.3}'
        assert _parse_json_field(output, 'mean_reward') == 194.2

    def test_json_field_multiline(self):
        output = 'some text\n{"score": 8.5}\nmore text'
        assert _parse_json_field(output, 'score') == 8.5

    def test_json_field_missing(self):
        output = '{"other": 42}'
        assert _parse_json_field(output, 'score') is None

    def test_pass_rate(self):
        output = '10 passed, 2 failed, 1 error'
        rate = _parse_pass_rate(output)
        assert rate == pytest.approx(10 / 13)

    def test_pass_rate_all_pass(self):
        output = '25 passed'
        assert _parse_pass_rate(output) == 1.0

    def test_pass_rate_no_tests(self):
        output = 'no tests ran'
        assert _parse_pass_rate(output) is None

    def test_parse_score_dispatch(self):
        assert _parse_score('result: 42.0', 'last_float') == 42.0
        assert _parse_score('{"val": 3.14}', 'json_field', 'val') == 3.14
        assert _parse_score('5 passed, 5 failed', 'pass_rate') == 0.5
        assert _parse_score('score=99', 'custom_regex', r'score=(\d+)') == 99.0


# ── LLM verdict parsing ────────────────────────────────────────────

class TestLLMVerdictParsing:
    def test_clean_json(self):
        v = _parse_llm_verdict('{"score": 7.5, "feedback": "Good structure"}')
        assert v.score == 7.5
        assert v.feedback == 'Good structure'

    def test_json_in_text(self):
        v = _parse_llm_verdict('Here is my evaluation:\n{"score": 8.0, "feedback": "Clear and concise"}\nDone.')
        assert v.score == 8.0

    def test_unparseable(self):
        v = _parse_llm_verdict('I think this is about a 7 out of 10')
        assert v.error == 'parse_failed'


# ── Convergence ─────────────────────────────────────────────────────

class TestConvergence:
    def test_budget_exhausted(self):
        config = JudgeLoopConfig(current_iteration=20, max_iterations=20, best_score=50.0)
        result = check_convergence(config)
        assert result is not None
        assert result.reason == 'budget_exhausted'
        assert not result.converged

    def test_consecutive_failures(self):
        config = JudgeLoopConfig(consecutive_failures=5, max_consecutive_failures=5, max_iterations=100)
        result = check_convergence(config)
        assert result is not None
        assert result.reason == 'consecutive_failures'

    def test_target_met_maximize(self):
        config = JudgeLoopConfig(
            direction='maximize', target_score=100.0, best_score=105.0,
            max_iterations=100,
        )
        result = check_convergence(config)
        assert result is not None
        assert result.converged
        assert result.reason == 'target_met'

    def test_target_met_minimize(self):
        config = JudgeLoopConfig(
            direction='minimize', target_score=50.0, best_score=45.0,
            max_iterations=100,
        )
        result = check_convergence(config)
        assert result is not None
        assert result.converged
        assert result.reason == 'target_met'

    def test_target_not_met(self):
        config = JudgeLoopConfig(
            direction='maximize', target_score=100.0, best_score=50.0,
            max_iterations=100,
        )
        result = check_convergence(config)
        assert result is None

    def test_plateau(self):
        iters = [Iteration(iteration=i, kept=False, status='discarded') for i in range(1, 6)]
        config = JudgeLoopConfig(
            max_iterations=100, plateau_window=5,
            iterations=iters, best_score=50.0,
        )
        result = check_convergence(config)
        assert result is not None
        assert result.reason == 'plateau'

    def test_no_plateau_if_mixed(self):
        iters = [
            Iteration(iteration=1, kept=True, status='kept'),
            Iteration(iteration=2, kept=False, status='discarded'),
            Iteration(iteration=3, kept=False, status='discarded'),
            Iteration(iteration=4, kept=False, status='discarded'),
            Iteration(iteration=5, kept=False, status='discarded'),
        ]
        config = JudgeLoopConfig(
            max_iterations=100, plateau_window=5,
            iterations=iters,
        )
        result = check_convergence(config)
        # Last 5 includes a kept — not a plateau
        assert result is None

    def test_not_converged_yet(self):
        config = JudgeLoopConfig(max_iterations=100, current_iteration=5)
        result = check_convergence(config)
        assert result is None


# ── Improvement check ───────────────────────────────────────────────

class TestImprovement:
    def test_maximize_improvement(self):
        assert is_improvement(110.0, 100.0, 'maximize')
        assert not is_improvement(90.0, 100.0, 'maximize')

    def test_minimize_improvement(self):
        assert is_improvement(40.0, 50.0, 'minimize')
        assert not is_improvement(60.0, 50.0, 'minimize')

    def test_min_delta(self):
        assert not is_improvement(100.1, 100.0, 'maximize', min_delta=0.5)
        assert is_improvement(100.6, 100.0, 'maximize', min_delta=0.5)

    def test_min_delta_rel_scales_with_magnitude(self):
        # Relative floor of 2% of |best|. At a large best, a tiny absolute gain
        # is rejected; the same 2% threshold accepts a proportional gain.
        assert not is_improvement(99.0, 100.0, 'minimize', min_delta_rel=0.02)   # 1% < 2%
        assert is_improvement(97.0, 100.0, 'minimize', min_delta_rel=0.02)       # 3% > 2%
        # The bug this fixes: an absolute delta sized for a large baseline goes
        # blind once the metric shrinks. A relative floor stays meaningful.
        assert not is_improvement(0.0076, 0.0076, 'minimize', min_delta=0.002)   # absolute: blind
        assert is_improvement(0.0066, 0.0076, 'minimize', min_delta_rel=0.02)    # relative: ~13% kept

    def test_min_delta_uses_larger_of_abs_and_rel(self):
        # Effective floor is max(abs, rel*|best|): both must be cleared.
        # rel floor = 0.05*100 = 5; abs floor = 2 → effective 5.
        assert not is_improvement(96.0, 100.0, 'minimize', min_delta=2.0, min_delta_rel=0.05)
        assert is_improvement(94.0, 100.0, 'minimize', min_delta=2.0, min_delta_rel=0.05)

    def test_nan_not_improvement(self):
        assert not is_improvement(float('nan'), 100.0, 'maximize')

    def test_equal_not_improvement(self):
        assert not is_improvement(100.0, 100.0, 'maximize')


# ── Persistence ─────────────────────────────────────────────────────

class TestPersistence:
    def test_create_and_load(self, tmp_path):
        config = create_loop(
            tmp_path,
            goal='optimize latency',
            project='/tmp/proj',
            agent_id='AG-001',
            judge_type='quantitative',
            eval_command='echo 42',
            max_iterations=10,
        )
        assert config.id.startswith('jl-')
        assert config.status == 'created'

        loaded = load_loop(tmp_path, config.id)
        assert loaded is not None
        assert loaded.goal == 'optimize latency'
        assert loaded.max_iterations == 10

    def test_save_and_reload_with_iterations(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project='/tmp', agent_id='AG-001',
        )
        config.iterations.append(Iteration(
            iteration=1, score=42.0, kept=True, status='kept',
            change_summary='test change',
        ))
        save_loop(tmp_path, config)

        loaded = load_loop(tmp_path, config.id)
        assert len(loaded.iterations) == 1
        assert loaded.iterations[0].score == 42.0
        assert loaded.iterations[0].kept is True

    def test_list_loops(self, tmp_path):
        create_loop(tmp_path, goal='loop1', project='/tmp', agent_id='AG-001')
        create_loop(tmp_path, goal='loop2', project='/tmp', agent_id='AG-001')

        loops = list_loops(tmp_path)
        assert len(loops) == 2

    def test_load_nonexistent(self, tmp_path):
        assert load_loop(tmp_path, 'jl-doesnotexist') is None


# ── Quantitative Judge ──────────────────────────────────────────────

class TestQuantitativeJudge:
    def test_simple_eval(self, tmp_path):
        config = JudgeLoopConfig(eval_command='echo 42.5', metric_name='score')
        judge = QuantitativeJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.score == 42.5
        assert verdict.error is None

    def test_eval_with_run_command(self, tmp_path):
        (tmp_path / 'output.txt').write_text('result: 99.9\n')
        config = JudgeLoopConfig(
            run_command='echo "running"',
            eval_command='cat output.txt',
            metric_name='score',
        )
        judge = QuantitativeJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.score == 99.9

    def test_eval_failure(self, tmp_path):
        config = JudgeLoopConfig(eval_command='exit 1', metric_name='score')
        judge = QuantitativeJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.error == 'eval_failed'

    def test_run_failure(self, tmp_path):
        config = JudgeLoopConfig(
            run_command='exit 1',
            eval_command='echo 42',
            metric_name='score',
        )
        judge = QuantitativeJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.error == 'run_failed'

    def test_parse_failure(self, tmp_path):
        config = JudgeLoopConfig(eval_command='echo "no numbers"', metric_name='score')
        judge = QuantitativeJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.error == 'parse_failed'

    def test_no_eval_command(self, tmp_path):
        config = JudgeLoopConfig(metric_name='score')
        judge = QuantitativeJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.error == 'no_eval'


# ── Correctness Judge ───────────────────────────────────────────────

class TestCorrectnessJudge:
    def test_all_pass(self, tmp_path):
        config = JudgeLoopConfig(eval_command='echo "10 passed"')
        judge = CorrectnessJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.score == 1.0

    def test_mixed_results(self, tmp_path):
        config = JudgeLoopConfig(eval_command='echo "8 passed, 2 failed"')
        judge = CorrectnessJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.score == pytest.approx(0.8)

    def test_with_errors(self, tmp_path):
        config = JudgeLoopConfig(eval_command='echo "5 passed, 3 failed, 2 error"')
        judge = CorrectnessJudge()
        verdict = judge.evaluate(config, tmp_path)
        assert verdict.score == pytest.approx(0.5)


# ── Composite Judge ─────────────────────────────────────────────────

class TestCompositeJudge:
    def test_weighted_average(self, tmp_path):
        j1 = QuantitativeJudge()
        j2 = QuantitativeJudge()

        config = JudgeLoopConfig(
            eval_command='echo 80',
            metric_name='score',
        )

        # Both judges evaluate the same command
        composite = CompositeJudge([(j1, 0.7), (j2, 0.3)])
        verdict = composite.evaluate(config, tmp_path)
        # Both return 80, so weighted average is 80
        assert verdict.score == pytest.approx(80.0)

    def test_no_judges(self, tmp_path):
        composite = CompositeJudge([])
        config = JudgeLoopConfig()
        verdict = composite.evaluate(config, tmp_path)
        assert verdict.error == 'no_judges'


# ── Factory ─────────────────────────────────────────────────────────

class TestFactory:
    def test_create_quantitative(self):
        config = JudgeLoopConfig(judge_type='quantitative')
        judge = create_judge(config)
        assert isinstance(judge, QuantitativeJudge)

    def test_create_correctness(self):
        config = JudgeLoopConfig(judge_type='correctness')
        judge = create_judge(config)
        assert isinstance(judge, CorrectnessJudge)

    def test_create_aesthetic(self):
        config = JudgeLoopConfig(judge_type='aesthetic')
        judge = create_judge(config)
        assert isinstance(judge, AestheticJudge)

    def test_create_composite(self):
        config = JudgeLoopConfig(
            judge_type='composite',
            sub_judges=[
                {'judge_type': 'quantitative', 'weight': 0.5, 'eval_command': 'echo 1'},
                {'judge_type': 'correctness', 'weight': 0.5, 'eval_command': 'echo "5 passed"'},
            ],
        )
        judge = create_judge(config)
        assert isinstance(judge, CompositeJudge)
        assert len(judge.sub_judges) == 2


# ── Baseline ────────────────────────────────────────────────────────

class TestBaseline:
    def test_run_baseline(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 100.0',
        )
        judge = QuantitativeJudge()
        config = run_baseline(config, judge, tmp_path)

        assert config.status == 'running'
        assert config.baseline == 100.0
        assert config.best_score == 100.0
        assert len(config.iterations) == 1
        assert config.iterations[0].iteration == 0
        assert config.iterations[0].kept is True

    def test_baseline_with_constraints(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 50',
            constraint_commands=['true'],  # passes
        )
        judge = QuantitativeJudge()
        config = run_baseline(config, judge, tmp_path)
        assert config.baseline == 50.0

    def test_baseline_constraint_failure(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 50',
            constraint_commands=['false'],  # fails
        )
        judge = QuantitativeJudge()
        config = run_baseline(config, judge, tmp_path)
        assert config.status == 'failed'


# ── Iteration ───────────────────────────────────────────────────────

class TestIteration:
    def test_improvement_kept(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 150.0', direction='maximize',
        )
        config.status = 'running'
        config.baseline = 100.0
        config.best_score = 100.0

        judge = QuantitativeJudge()
        config, iteration = run_iteration(
            config, judge, tmp_path,
            change_summary='improved the thing',
        )

        assert iteration.kept is True
        assert iteration.status == 'kept'
        assert iteration.score == 150.0
        assert config.best_score == 150.0
        assert config.current_iteration == 1

    def test_regression_discarded(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 80.0', direction='maximize',
        )
        config.status = 'running'
        config.baseline = 100.0
        config.best_score = 100.0

        judge = QuantitativeJudge()
        config, iteration = run_iteration(
            config, judge, tmp_path,
            change_summary='made things worse',
        )

        assert iteration.kept is False
        assert iteration.status == 'discarded'
        assert config.best_score == 100.0  # unchanged
        assert config.consecutive_failures == 1

    def test_minimize_direction(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 40.0', direction='minimize',
        )
        config.status = 'running'
        config.baseline = 50.0
        config.best_score = 50.0

        judge = QuantitativeJudge()
        config, iteration = run_iteration(
            config, judge, tmp_path,
            change_summary='reduced latency',
        )

        assert iteration.kept is True
        assert config.best_score == 40.0

    def test_constraint_failure(self, tmp_path):
        config = create_loop(
            tmp_path, goal='test', project=str(tmp_path), agent_id='AG-001',
            eval_command='echo 200', direction='maximize',
            constraint_commands=['false'],  # always fails
        )
        config.status = 'running'
        config.baseline = 100.0
        config.best_score = 100.0

        judge = QuantitativeJudge()
        config, iteration = run_iteration(
            config, judge, tmp_path,
            change_summary='broke constraints',
        )

        assert iteration.kept is False
        assert iteration.status == 'constraint_failed'
        assert config.consecutive_failures == 1

    def test_iteration_with_checkpoint(self, tmp_path):
        from charon.automation.checkpoint_manager import CheckpointManager

        work_dir = tmp_path / 'work'
        work_dir.mkdir()
        (work_dir / 'code.py').write_text('x = 1\n')
        state_dir = tmp_path / 'state'
        state_dir.mkdir()

        cp_mgr = CheckpointManager(state_dir, work_dir)
        best_cp = cp_mgr.snapshot('best')

        config = create_loop(
            state_dir, goal='test', project=str(work_dir), agent_id='AG-001',
            eval_command='echo 80', direction='maximize',
        )
        config.status = 'running'
        config.baseline = 100.0
        config.best_score = 100.0
        config.best_checkpoint = best_cp

        # Modify the file (simulating an implementer shade)
        (work_dir / 'code.py').write_text('x = 999  # bad change\n')

        judge = QuantitativeJudge()
        config, iteration = run_iteration(
            config, judge, work_dir,
            change_summary='bad change',
            checkpoint_mgr=cp_mgr,
        )

        # Should be discarded and rolled back
        assert iteration.kept is False
        assert (work_dir / 'code.py').read_text() == 'x = 1\n'  # rolled back


# ── Iteration prompt ────────────────────────────────────────────────

class TestIterationPrompt:
    def test_basic_prompt(self):
        config = JudgeLoopConfig(
            goal='optimize latency',
            metric_name='p99_ms',
            direction='minimize',
            baseline=120.0,
            best_score=80.0,
            best_iteration=3,
            current_iteration=5,
            max_iterations=20,
            scope=['src/api.py'],
            program='Focus on query batching.',
        )
        prompt = build_iteration_prompt(config)

        assert 'Iteration 6' in prompt
        assert 'p99_ms' in prompt
        assert 'minimize' in prompt
        assert 'Baseline: 120.0' in prompt
        assert 'Current best: 80.0' in prompt
        assert 'query batching' in prompt
        assert 'src/api.py' in prompt

    def test_prompt_with_history(self):
        config = JudgeLoopConfig(
            current_iteration=3,
            max_iterations=20,
            iterations=[
                Iteration(iteration=1, score=100.0, kept=True, status='kept', change_summary='first try'),
                Iteration(iteration=2, score=95.0, kept=False, status='discarded',
                          change_summary='bad idea', feedback='regression on queries'),
                Iteration(iteration=3, score=110.0, kept=True, status='kept', change_summary='good change'),
            ],
        )
        prompt = build_iteration_prompt(config)
        assert 'first try' in prompt
        assert 'bad idea' in prompt
        assert 'DISCARDED' in prompt
        assert 'KEPT' in prompt


# ── Format status ───────────────────────────────────────────────────

class TestFormatStatus:
    def test_created_status(self):
        config = JudgeLoopConfig(goal='test', status='created')
        output = format_status(config)
        assert 'created' in output

    def test_running_status(self):
        config = JudgeLoopConfig(
            id='jl-test',
            goal='optimize speed',
            status='running',
            baseline=100.0,
            best_score=150.0,
            best_iteration=3,
            current_iteration=5,
            max_iterations=20,
            direction='maximize',
            iterations=[
                Iteration(iteration=0, score=100.0, kept=True, status='kept', change_summary='baseline'),
                Iteration(iteration=1, score=120.0, kept=True, status='kept', change_summary='LR schedule'),
                Iteration(iteration=2, score=115.0, kept=False, status='discarded', change_summary='batch size'),
            ],
        )
        output = format_status(config)
        assert 'optimize speed' in output
        assert '100.0' in output
        assert '150.0' in output
        assert 'LR schedule' in output
