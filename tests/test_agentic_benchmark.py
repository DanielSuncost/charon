from __future__ import annotations

import json

import pytest

from charon.evaluation import (
    AgenticBenchmark,
    BenchmarkTrial,
    paired_bootstrap_delta,
    pareto_frontier,
    routing_matrix_data,
    summarize_policy_trials,
)


def _trials() -> list[BenchmarkTrial]:
    outcomes = {
        "baseline": [
            ("s1", False, 0.2, 0.10, 300.0, "small", "large"),
            ("s2", True, 0.8, 0.12, 400.0, "large", "large"),
            ("s3", False, 0.4, 0.11, 350.0, "small", "small"),
        ],
        "contender": [
            ("s1", True, 0.9, 0.20, 250.0, "large", "large"),
            ("s2", True, 0.9, 0.22, 300.0, "large", "large"),
            ("s3", False, 0.5, 0.18, 325.0, "small", "small"),
        ],
    }
    return [
        BenchmarkTrial(
            scenario_id=scenario_id,
            policy_id=policy_id,
            success=success,
            score=score,
            cost_usd=cost,
            latency_ms=latency,
            selected_model_id=selected,
            oracle_model_id=oracle,
            task_family="coding",
        )
        for policy_id, rows in outcomes.items()
        for scenario_id, success, score, cost, latency, selected, oracle in rows
    ]


def test_policy_summary_includes_wilson_and_resource_metrics() -> None:
    summaries = summarize_policy_trials(_trials())
    baseline = summaries["baseline"]

    assert baseline.trial_count == 3
    assert baseline.success_count == 1
    assert baseline.success_rate == pytest.approx(1 / 3)
    assert baseline.success_ci_low < baseline.success_rate
    assert baseline.success_ci_high > baseline.success_rate
    assert baseline.mean_score == pytest.approx((0.2 + 0.8 + 0.4) / 3)
    assert baseline.mean_cost_usd == pytest.approx(0.11)
    assert baseline.p50_latency_ms == pytest.approx(350)
    assert baseline.p95_latency_ms == pytest.approx(395)


def test_paired_bootstrap_is_reproducible_and_preserves_pairing() -> None:
    kwargs = {
        "baseline_policy_id": "baseline",
        "contender_policy_id": "contender",
        "metric": "success",
        "bootstrap_samples": 500,
        "seed": 42,
    }

    first = paired_bootstrap_delta(_trials(), **kwargs)
    second = paired_bootstrap_delta(reversed(_trials()), **kwargs)

    assert first == second
    assert first.pair_count == 3
    assert first.matched_pair_count == 3
    assert first.baseline_unmatched_count == 0
    assert first.contender_unmatched_count == 0
    assert first.scenario_count == 3
    assert first.mean_delta == pytest.approx(1 / 3)
    assert first.higher_is_better is True
    assert first.ci_low <= first.mean_delta <= first.ci_high


def test_pareto_frontier_excludes_dominated_policies() -> None:
    summaries = {
        "quality": {
            "success_rate": 0.95,
            "mean_score": 0.90,
            "mean_cost_usd": 1.00,
            "p95_latency_ms": 1_000,
        },
        "economy": {
            "success_rate": 0.80,
            "mean_score": 0.75,
            "mean_cost_usd": 0.10,
            "p95_latency_ms": 500,
        },
        "dominated": {
            "success_rate": 0.70,
            "mean_score": 0.60,
            "mean_cost_usd": 0.20,
            "p95_latency_ms": 600,
        },
    }

    assert pareto_frontier(summaries) == ("economy", "quality")


def test_routing_matrix_exposes_policy_counts_and_oracle_confusion() -> None:
    matrix = routing_matrix_data(_trials())

    assert matrix["policy_ids"] == ["baseline", "contender"]
    assert matrix["model_ids"] == ["large", "small"]
    assert matrix["policy_model_matrix"] == [[1, 2], [2, 1]]
    assert matrix["oracle_trial_count"] == 6
    assert matrix["oracle_match_count"] == 5
    assert matrix["oracle_accuracy"] == pytest.approx(5 / 6)
    assert matrix["oracle_selected_counts"] == {
        "large": {"large": 3, "small": 1},
        "small": {"large": 0, "small": 2},
    }


def test_benchmark_report_is_deterministic_and_json_exportable(tmp_path) -> None:
    benchmark = AgenticBenchmark(_trials())
    first = benchmark.report(
        baseline_policy_id="baseline",
        bootstrap_samples=200,
        seed=7,
    )
    second = benchmark.report(
        baseline_policy_id="baseline",
        bootstrap_samples=200,
        seed=7,
    )

    assert first == second
    assert len(first.paired_deltas) == 4
    assert set(first.pareto_policy_ids) == {"baseline", "contender"}
    encoded = first.to_json()
    assert json.loads(encoded)["config"]["seed"] == 7
    output = first.write_json(tmp_path / "report.json")
    assert json.loads(output.read_text()) == json.loads(encoded)


@pytest.mark.parametrize("success", ["false", "true", 0, 1, None])
def test_benchmark_trial_requires_a_json_boolean(success: object) -> None:
    payload = {
        "scenario_id": "s",
        "policy_id": "p",
        "success": success,
        "score": 0.5,
        "cost_usd": 0.1,
        "latency_ms": 10,
    }

    with pytest.raises(ValueError, match="success must be a boolean"):
        BenchmarkTrial.from_dict(payload)

    with pytest.raises(ValueError, match="success must be a boolean"):
        BenchmarkTrial(
            scenario_id="s",
            policy_id="p",
            success=success,  # type: ignore[arg-type]
            score=0.5,
            cost_usd=0.1,
            latency_ms=10,
        )


def test_policy_summary_clusters_repetitions_by_scenario() -> None:
    repeated = [
        BenchmarkTrial(
            scenario_id="frequent",
            pair_id=str(index),
            policy_id="p",
            success=True,
            score=1.0,
            cost_usd=1.0,
            latency_ms=100,
        )
        for index in range(10)
    ]
    repeated.append(
        BenchmarkTrial(
            scenario_id="rare",
            policy_id="p",
            success=False,
            score=0.0,
            cost_usd=0.0,
            latency_ms=200,
        )
    )

    forward = summarize_policy_trials(
        repeated, bootstrap_samples=500, seed=9
    )["p"]
    reverse = summarize_policy_trials(
        reversed(repeated), bootstrap_samples=500, seed=9
    )["p"]

    assert forward == reverse
    assert forward.trial_count == 11
    assert forward.scenario_count == 2
    assert forward.trial_success_rate == pytest.approx(10 / 11)
    assert forward.success_rate == pytest.approx(0.5)
    assert forward.mean_score == pytest.approx(0.5)
    assert forward.mean_cost_usd == pytest.approx(0.5)
    assert forward.interval_method == "scenario_cluster_bootstrap_percentile"
    assert forward.interval_status == "estimated"
    assert forward.success_ci_low == pytest.approx(0.0)
    assert forward.success_ci_high == pytest.approx(1.0)


def test_policy_summary_marks_one_scenario_as_insufficient_for_cluster_ci() -> None:
    trial = BenchmarkTrial(
        scenario_id="only",
        policy_id="p",
        success=True,
        score=1.0,
        cost_usd=0.1,
        latency_ms=10,
    )

    summary = summarize_policy_trials([trial])["p"]

    assert summary.scenario_count == 1
    assert summary.interval_status == "insufficient_scenarios"
    assert summary.success_ci_low is None
    assert summary.success_ci_high is None
    assert summary.trial_success_ci_low < summary.trial_success_rate


def _partially_paired_trials() -> list[BenchmarkTrial]:
    return [
        BenchmarkTrial("s1", "base", True, 1.0, 0.1, 10, pair_id="0"),
        BenchmarkTrial("s2", "base", False, 0.0, 0.1, 10, pair_id="0"),
        BenchmarkTrial("s3", "base", True, 1.0, 0.1, 10, pair_id="0"),
        BenchmarkTrial("s1", "new", True, 1.0, 0.1, 10, pair_id="0"),
        BenchmarkTrial("s2", "new", True, 1.0, 0.1, 10, pair_id="0"),
        BenchmarkTrial("s4", "new", False, 0.0, 0.1, 10, pair_id="0"),
    ]


def test_paired_delta_reports_missing_pairs_or_rejects_them_explicitly() -> None:
    delta = paired_bootstrap_delta(
        _partially_paired_trials(),
        baseline_policy_id="base",
        contender_policy_id="new",
        bootstrap_samples=200,
        seed=4,
    )

    assert delta.baseline_trial_count == 3
    assert delta.contender_trial_count == 3
    assert delta.matched_pair_count == 2
    assert delta.baseline_unmatched_count == 1
    assert delta.contender_unmatched_count == 1
    assert delta.missing_pair_behavior == "report"

    with pytest.raises(ValueError, match="coverage differs"):
        paired_bootstrap_delta(
            _partially_paired_trials(),
            baseline_policy_id="base",
            contender_policy_id="new",
            missing_pair_behavior="error",
        )


def test_paired_delta_rejects_task_family_mismatch() -> None:
    trials = [
        BenchmarkTrial(
            "s1",
            "base",
            True,
            1.0,
            0.1,
            10,
            task_family="coding",
        ),
        BenchmarkTrial(
            "s1",
            "new",
            True,
            1.0,
            0.1,
            10,
            task_family="research",
        ),
    ]

    with pytest.raises(ValueError, match="task_family"):
        paired_bootstrap_delta(
            trials,
            baseline_policy_id="base",
            contender_policy_id="new",
        )


def test_paired_delta_rejects_seed_mismatch() -> None:
    trials = [
        BenchmarkTrial(
            "s1",
            "base",
            True,
            1.0,
            0.1,
            10,
            metadata={"seed": 11},
        ),
        BenchmarkTrial(
            "s1",
            "new",
            True,
            1.0,
            0.1,
            10,
            metadata={"seed": 12},
        ),
    ]

    with pytest.raises(ValueError, match="metadata.seed"):
        paired_bootstrap_delta(
            trials,
            baseline_policy_id="base",
            contender_policy_id="new",
        )


def test_report_exports_pairing_diagnostics_and_missing_pair_contract() -> None:
    report = AgenticBenchmark(_partially_paired_trials()).report(
        baseline_policy_id="base",
        bootstrap_samples=100,
        seed=3,
    )
    pairing = report.to_dict()["pairing"]

    assert pairing["missing_pair_behavior"] == "report"
    assert pairing["comparisons"]["new"] == {
        "baseline_trial_count": 3,
        "contender_trial_count": 3,
        "matched_pair_count": 2,
        "baseline_unmatched_count": 1,
        "contender_unmatched_count": 1,
        "matched_scenario_count": 2,
    }
    assert report.config["missing_pair_behavior"] == "report"
