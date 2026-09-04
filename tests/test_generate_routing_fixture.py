from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from charon.evaluation import AgenticBenchmark, BenchmarkTrial
from charon.routing import (
    ModelCandidate,
    TrialRecord,
    aggregate_calibration_by_task,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_routing_fixture.py"
ROUTING_DIR = REPO_ROOT / "examples" / "routing"
ARTIFACT_NAMES = (
    "calibration-trials.jsonl",
    "evaluation-model-outcomes.jsonl",
    "policy-benchmark-trials.jsonl",
    "calibration-fixture.json",
)


def _run_generator(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_checked_in_routing_fixture_is_byte_reproducible() -> None:
    result = _run_generator("--check")

    assert result.returncode == 0, result.stderr
    assert "reproducible and up to date" in result.stdout


def test_generator_recreates_raw_corpora_and_aggregate_fixture(tmp_path) -> None:
    result = _run_generator("--output-dir", tmp_path)

    assert result.returncode == 0, result.stderr
    for name in ARTIFACT_NAMES:
        assert (tmp_path / name).read_bytes() == (ROUTING_DIR / name).read_bytes()

    fixture = json.loads((tmp_path / "calibration-fixture.json").read_text())
    assert {
        "schema_version",
        "dataset_id",
        "data_kind",
        "seed",
        "description",
        "task_classes",
        "models",
        "policies",
        "recommendation",
        "comparisons",
        "routing_matrix",
        "provenance",
    } == set(fixture)
    assert fixture["data_kind"] == "generated_fixture"
    assert fixture["seed"] == 42
    assert "synthetic" in fixture["description"].lower()
    assert "not live provider measurements" in fixture["description"].lower()

    recommendation = fixture["recommendation"]
    assert recommendation["rule_id"] == "held_out_operating_envelope_v1"
    assert recommendation["evaluation_split"] == "evaluation"
    assert recommendation["rank_by"] == [
        "success_rate_desc",
        "mean_score_desc",
        "policy_id_asc",
    ]
    eligibility = recommendation["eligibility"]
    eligible = [
        policy
        for policy in fixture["policies"]
        if policy["mean_cost_usd"] <= eligibility["mean_cost_usd_lte"]
        and policy["p95_latency_ms"] <= eligibility["p95_latency_ms_lte"]
    ]
    independently_selected = min(
        eligible,
        key=lambda policy: (
            -policy["success_rate"],
            -policy["mean_score"],
            policy["policy_id"],
        ),
    )["policy_id"]
    assert recommendation["eligible_policy_ids"] == sorted(
        policy["policy_id"] for policy in eligible
    )
    assert recommendation["selected_policy_id"] == independently_selected
    assert independently_selected == "balanced"
    assert [
        policy["policy_id"]
        for policy in fixture["policies"]
        if policy.get("recommended") is True
    ] == [independently_selected]
    assert all(
        "recommended" not in policy
        for policy in fixture["policies"]
        if policy["policy_id"] != independently_selected
    )

    provenance = fixture["provenance"]
    assert provenance["generation_method"] == "seeded_synthetic_simulation"
    assert provenance["calibration_scenario_count"] == 32
    assert provenance["evaluation_scenario_count"] == 32
    assert provenance["repetitions_per_scenario"] == 3
    assert len(provenance["config_sha256"]) == 64
    for relative_path, digest in provenance["code_sha256"].items():
        assert _sha256(REPO_ROOT / relative_path) == digest

    model_rows = _jsonl(tmp_path / "calibration-trials.jsonl")
    evaluation_rows = _jsonl(tmp_path / "evaluation-model-outcomes.jsonl")
    policy_rows = _jsonl(tmp_path / "policy-benchmark-trials.jsonl")
    raw_corpora = provenance["raw_corpora"]
    assert len(model_rows) == raw_corpora["model_calibration_trials"]["records"]
    assert (
        len(evaluation_rows)
        == raw_corpora["evaluation_model_outcomes"]["records"]
    )
    assert len(policy_rows) == raw_corpora["policy_benchmark_trials"]["records"]
    assert (
        _sha256(tmp_path / "calibration-trials.jsonl")
        == raw_corpora["model_calibration_trials"]["sha256"]
    )
    assert (
        _sha256(tmp_path / "evaluation-model-outcomes.jsonl")
        == raw_corpora["evaluation_model_outcomes"]["sha256"]
    )
    assert (
        _sha256(tmp_path / "policy-benchmark-trials.jsonl")
        == raw_corpora["policy_benchmark_trials"]["sha256"]
    )

    model_trials = [TrialRecord.from_dict(row) for row in model_rows]
    evaluation_outcomes = [
        TrialRecord.from_dict(row) for row in evaluation_rows
    ]
    policy_trials = [BenchmarkTrial.from_dict(row) for row in policy_rows]
    assert len(model_trials) == 288
    assert len(evaluation_outcomes) == 288
    assert len(policy_trials) == 384
    assert {
        trial.metadata["data_kind"] for trial in model_trials
    } == {"synthetic_trial"}
    assert {trial.metadata["split"] for trial in model_trials} == {
        "calibration"
    }
    assert {
        trial.metadata["split"] for trial in evaluation_outcomes
    } == {"evaluation"}
    assert {
        trial.metadata["data_kind"] for trial in policy_trials
    } == {"synthetic_trial"}
    assert {trial.metadata["split"] for trial in policy_trials} == {
        "evaluation"
    }

    calibration_ids = {trial.task_id for trial in model_trials}
    evaluation_ids = {trial.task_id for trial in evaluation_outcomes}
    policy_ids = {trial.scenario_id for trial in policy_trials}
    assert calibration_ids.isdisjoint(evaluation_ids)
    assert calibration_ids.isdisjoint(policy_ids)
    assert policy_ids == evaluation_ids
    assert all(task_id.startswith("cal-") for task_id in calibration_ids)
    assert all(task_id.startswith("eval-") for task_id in evaluation_ids)

    counterfactuals = Counter(
        (
            trial.task_family,
            trial.task_id,
            trial.metadata["repetition"],
        )
        for trial in evaluation_outcomes
    )
    assert set(counterfactuals.values()) == {3}
    outcome_by_key = {
        (
            trial.model_id,
            trial.task_family,
            trial.task_id,
            trial.metadata["repetition"],
        ): trial
        for trial in evaluation_outcomes
    }
    for trial in policy_trials:
        selected = outcome_by_key[
            (
                trial.selected_model_id,
                trial.task_family,
                trial.scenario_id,
                trial.metadata["repetition"],
            )
        ]
        assert trial.success is selected.success
        assert trial.score == selected.score
        assert trial.cost_usd == selected.cost_usd
        assert trial.latency_ms == selected.latency_ms

    repetitions = Counter(
        (trial.policy_id, trial.scenario_id) for trial in policy_trials
    )
    assert set(repetitions.values()) == {3}
    assert len(repetitions) == 4 * 32

    grid = aggregate_calibration_by_task(model_trials)
    fixture_models = {
        model["model_id"]: model for model in fixture["models"]
    }
    assert set(fixture_models) == {"ember-small", "forge-medium", "oracle-large"}
    for model_id, model in fixture_models.items():
        ModelCandidate.from_dict(model)
        assert set(model["task_calibrations"]) == {
            "planning",
            "implementation",
            "interface",
            "verification",
        }
        for task_family, profile in grid[model_id].items():
            generated = model["task_calibrations"][task_family]
            assert generated["trial_count"] == profile.trial_count
            assert generated["success_count"] == profile.success_count
            assert generated["success_rate"] == pytest.approx(
                profile.success_rate, abs=1e-6
            )

    report = AgenticBenchmark(policy_trials).report(
        baseline_policy_id="balanced",
        bootstrap_samples=4_000,
        seed=42,
        missing_pair_behavior="error",
    )
    fixture_policies = {
        policy["policy_id"]: policy for policy in fixture["policies"]
    }
    for policy_id, summary in report.summaries.items():
        generated = fixture_policies[policy_id]
        assert generated["trials"] == summary.trial_count
        assert generated["scenarios"] == summary.scenario_count
        assert generated["success_rate"] == pytest.approx(
            summary.success_rate, abs=1e-6
        )
        assert generated["interval_method"] == summary.interval_method
    assert all(
        comparison["matched_pairs"] == 96
        and comparison["matched_scenarios"] == 32
        and comparison["unmatched_baseline"] == 0
        and comparison["unmatched_challenger"] == 0
        for comparison in fixture["comparisons"]
    )


def test_check_mode_detects_drift_without_rewriting(tmp_path) -> None:
    generated = _run_generator("--output-dir", tmp_path)
    assert generated.returncode == 0, generated.stderr
    fixture_path = tmp_path / "calibration-fixture.json"
    drifted = fixture_path.read_bytes() + b" "
    fixture_path.write_bytes(drifted)

    checked = _run_generator("--check", "--output-dir", tmp_path)

    assert checked.returncode == 1
    assert "out of date" in checked.stderr
    assert fixture_path.read_bytes() == drifted
