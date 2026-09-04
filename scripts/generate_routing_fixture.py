#!/usr/bin/env python3
"""Generate the deterministic routing calibration demonstration corpus.

The checked-in aggregates are derived exclusively from the checked-in JSONL
trial records. All measurements are synthetic and are labeled accordingly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from charon.evaluation import AgenticBenchmark, BenchmarkTrial  # noqa: E402
from charon.routing import (  # noqa: E402
    ModelCandidate,
    TaskProfile,
    TrialRecord,
    aggregate_calibration,
    aggregate_calibration_by_task,
    route_models,
)


SEED = 42
GENERATOR_VERSION = "1.0"
SCENARIOS_PER_FAMILY = 8
REPETITIONS = 3
BOOTSTRAP_SAMPLES = 4_000
CONFIDENCE_LEVEL = 0.95
MODEL_CORPUS_NAME = "calibration-trials.jsonl"
EVALUATION_OUTCOMES_NAME = "evaluation-model-outcomes.jsonl"
POLICY_CORPUS_NAME = "policy-benchmark-trials.jsonl"
FIXTURE_NAME = "calibration-fixture.json"
TASK_FAMILIES = ("planning", "implementation", "interface", "verification")
POLICIES = ("balanced", "quality", "economy", "deadline")
RECOMMENDATION_RULE = {
    "rule_id": "held_out_operating_envelope_v1",
    "evaluation_split": "evaluation",
    "eligibility": {
        "mean_cost_usd_lte": 0.03,
        "p95_latency_ms_lte": 25_000,
    },
    "rank_by": [
        "success_rate_desc",
        "mean_score_desc",
        "policy_id_asc",
    ],
}

MODEL_CONFIG: tuple[dict[str, Any], ...] = (
    {
        "candidate_id": "ember-small",
        "provider": "local",
        "model_id": "ember-small",
        "capabilities": ("code", "tools"),
        "context_window": 65_536,
        "estimated_cost_usd": 0.004,
        "estimated_latency_ms": 8_200,
        "estimated_quality": 0.74,
        "estimated_reliability": 0.82,
        "latency_ms": 7_900,
        "latency_multiplier": {
            "planning": 1.65,
            "implementation": 1.0,
            "interface": 1.0,
            "verification": 1.0,
        },
        "cost_usd": 0.0038,
        "calibration_bias": 0.035,
        "performance": {
            "planning": (0.80, 0.78),
            "implementation": (0.76, 0.76),
            "interface": (0.58, 0.64),
            "verification": (0.88, 0.84),
        },
    },
    {
        "candidate_id": "forge-medium",
        "provider": "hosted",
        "model_id": "forge-medium",
        "capabilities": ("code", "tools", "vision"),
        "context_window": 131_072,
        "estimated_cost_usd": 0.031,
        "estimated_latency_ms": 15_100,
        "estimated_quality": 0.86,
        "estimated_reliability": 0.91,
        "latency_ms": 14_800,
        "latency_multiplier": {
            "planning": 0.78,
            "implementation": 1.0,
            "interface": 1.0,
            "verification": 1.0,
        },
        "cost_usd": 0.0304,
        "calibration_bias": 0.005,
        "performance": {
            "planning": (0.995, 0.91),
            "implementation": (0.96, 0.91),
            "interface": (0.87, 0.87),
            "verification": (0.91, 0.89),
        },
    },
    {
        "candidate_id": "oracle-large",
        "provider": "hosted",
        "model_id": "oracle-large",
        "capabilities": ("code", "long_context", "tools", "vision"),
        "context_window": 262_144,
        "estimated_cost_usd": 0.092,
        "estimated_latency_ms": 26_400,
        "estimated_quality": 0.94,
        "estimated_reliability": 0.95,
        "latency_ms": 25_800,
        "latency_multiplier": {
            "planning": 1.0,
            "implementation": 1.0,
            "interface": 1.0,
            "verification": 1.0,
        },
        "cost_usd": 0.0907,
        "calibration_bias": -0.015,
        "performance": {
            "planning": (0.96, 0.94),
            "implementation": (0.98, 0.95),
            "interface": (0.99, 0.99),
            "verification": (0.95, 0.93),
        },
    },
)

SIMULATION_CONFIG = {
    "seed": SEED,
    "generator_version": GENERATOR_VERSION,
    "calibration_scenarios_per_task_family": SCENARIOS_PER_FAMILY,
    "evaluation_scenarios_per_task_family": SCENARIOS_PER_FAMILY,
    "repetitions_per_scenario": REPETITIONS,
    "bootstrap_samples": BOOTSTRAP_SAMPLES,
    "confidence_level": CONFIDENCE_LEVEL,
    "task_families": list(TASK_FAMILIES),
    "policies": list(POLICIES),
    "recommendation_rule": RECOMMENDATION_RULE,
    "models": [
        {
            key: value
            for key, value in model.items()
            if key not in {"performance"}
        }
        | {
            "performance": {
                family: list(values)
                for family, values in model["performance"].items()
            }
        }
        for model in MODEL_CONFIG
    ],
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_rng(*parts: object) -> random.Random:
    material = ":".join([str(SEED), *(str(part) for part in parts)])
    seed = int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _round(value: float, digits: int = 6) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == -0.0 else rounded


def _round_tree(value: Any) -> Any:
    if isinstance(value, float):
        return _round(value)
    if isinstance(value, list):
        return [_round_tree(item) for item in value]
    if isinstance(value, tuple):
        return [_round_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_tree(item) for key, item in value.items()}
    return value


def _scenario_difficulty(index: int) -> float:
    return (index + 1) / (SCENARIOS_PER_FAMILY + 1)


def _scenario_id(split: str, task_family: str, index: int) -> str:
    prefix = {"calibration": "cal", "evaluation": "eval"}[split]
    return f"{prefix}-{task_family}-{index + 1:02d}"


def _task_profile(
    task_family: str,
    index: int,
    *,
    split: str = "evaluation",
) -> TaskProfile:
    difficulty = _scenario_difficulty(index)
    common: dict[str, Any] = {
        "task_id": _scenario_id(split, task_family, index),
        "task_family": task_family,
        "complexity": difficulty,
        "context_tokens": int(8_000 + difficulty * 28_000),
        "metadata": {
            "data_kind": "synthetic_scenario",
            "scenario_index": index,
            "difficulty": _round(difficulty),
        },
    }
    if task_family == "planning":
        common.update(
            required_capabilities=("tools",),
            quality_floor=0.55 + 0.02 * difficulty,
            max_cost_usd=(0.01 if index < 2 else 0.05 if index < 4 else None),
        )
    elif task_family == "implementation":
        common.update(
            required_capabilities=("code", "tools"),
            quality_floor=0.50 + 0.03 * difficulty,
            max_cost_usd=(0.04 if index < 2 else None),
        )
    elif task_family == "interface":
        common.update(
            required_capabilities=("tools", "vision"),
            quality_floor=0.68 + 0.04 * difficulty,
            max_cost_usd=(0.05 if index < 3 else None),
        )
    else:
        common.update(
            required_capabilities=("code", "tools"),
            quality_floor=0.58 + 0.06 * difficulty,
            latency_slo_ms=30_000,
            max_cost_usd=(0.01 if index < 3 else 0.05 if index < 5 else None),
        )
    return TaskProfile(**common)


def generate_model_trials(*, split: str) -> list[TrialRecord]:
    """Generate stable all-model outcomes for one disjoint data split."""

    if split not in {"calibration", "evaluation"}:
        raise ValueError("split must be calibration or evaluation")

    records: list[TrialRecord] = []
    for model in MODEL_CONFIG:
        model_id = model["model_id"]
        for task_family in TASK_FAMILIES:
            success_base, quality_base = model["performance"][task_family]
            for scenario_index in range(SCENARIOS_PER_FAMILY):
                difficulty = _scenario_difficulty(scenario_index)
                actual_probability = _clamp(
                    success_base - 0.16 * (difficulty - 0.5), 0.05, 0.995
                )
                predicted_probability = _clamp(
                    actual_probability + model["calibration_bias"], 0.02, 0.99
                )
                for repetition in range(REPETITIONS):
                    rng = _stable_rng(
                        "model",
                        split,
                        model_id,
                        task_family,
                        scenario_index,
                        repetition,
                    )
                    success = rng.random() < actual_probability
                    score = _clamp(
                        quality_base
                        - 0.10 * (difficulty - 0.5)
                        + (0.025 if success else -0.11)
                        + rng.gauss(0.0, 0.025)
                    )
                    latency = max(
                        1.0,
                        model["latency_ms"]
                        * model["latency_multiplier"][task_family]
                        * (
                            0.90
                            + 0.24 * difficulty
                            + rng.gauss(0.0, 0.055)
                        ),
                    )
                    cost = max(
                        0.0,
                        model["cost_usd"]
                        * (
                            0.94
                            + 0.16 * difficulty
                            + rng.gauss(0.0, 0.025)
                        ),
                    )
                    records.append(
                        TrialRecord(
                            model_id=model_id,
                            success=success,
                            latency_ms=_round(latency, 3),
                            cost_usd=_round(cost, 6),
                            score=_round(score),
                            predicted_success=_round(predicted_probability),
                            task_id=_scenario_id(
                                split, task_family, scenario_index
                            ),
                            task_family=task_family,
                            metadata={
                                "data_kind": "synthetic_trial",
                                "split": split,
                                "seed": SEED,
                                "scenario_index": scenario_index,
                                "repetition": repetition,
                                "difficulty": _round(difficulty),
                            },
                        )
                    )
    return records


def _build_candidates(
    model_trials: list[TrialRecord],
) -> list[ModelCandidate]:
    global_profiles = aggregate_calibration(model_trials)
    task_profiles = aggregate_calibration_by_task(model_trials)
    return [
        ModelCandidate(
            candidate_id=model["candidate_id"],
            provider=model["provider"],
            model_id=model["model_id"],
            capabilities=tuple(model["capabilities"]),
            context_window=model["context_window"],
            estimated_cost_usd=model["estimated_cost_usd"],
            estimated_latency_ms=model["estimated_latency_ms"],
            estimated_quality=model["estimated_quality"],
            estimated_reliability=model["estimated_reliability"],
            calibration=global_profiles[model["model_id"]],
            task_calibrations=task_profiles[model["model_id"]],
            metadata={
                "data_kind": "synthetic_profile",
                "seed": SEED,
            },
        )
        for model in MODEL_CONFIG
    ]


def generate_policy_trials(
    evaluation_outcomes: list[TrialRecord],
    candidates: list[ModelCandidate],
) -> list[BenchmarkTrial]:
    """Route every policy over held-out paired scenario repetitions."""

    outcomes = {
        (
            record.model_id,
            record.task_family,
            record.task_id,
            int(record.metadata["repetition"]),
        ): record
        for record in evaluation_outcomes
    }
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    records: list[BenchmarkTrial] = []
    for policy in POLICIES:
        for task_family in TASK_FAMILIES:
            for scenario_index in range(SCENARIOS_PER_FAMILY):
                task = _task_profile(
                    task_family, scenario_index, split="evaluation"
                )
                decision = route_models(task, candidates, policy=policy)
                if not decision.feasible or decision.selected_candidate_id is None:
                    raise RuntimeError(
                        f"{policy} has no route for {task.task_id}"
                    )
                selected_id = decision.selected_candidate_id
                feasible_ids = {
                    rationale.candidate_id
                    for rationale in decision.candidates
                    if rationale.feasible
                }
                if selected_id not in candidate_ids:
                    raise RuntimeError(f"unknown selected model {selected_id!r}")
                selected_rationale = next(
                    rationale
                    for rationale in decision.candidates
                    if rationale.candidate_id == selected_id
                )
                for repetition in range(REPETITIONS):
                    selected = outcomes[
                        (
                            selected_id,
                            task_family,
                            task.task_id,
                            repetition,
                        )
                    ]
                    feasible_outcomes = [
                        outcomes[
                            (
                                candidate_id,
                                task_family,
                                task.task_id,
                                repetition,
                            )
                        ]
                        for candidate_id in sorted(feasible_ids)
                    ]
                    oracle = max(
                        feasible_outcomes,
                        key=lambda record: (record.score or 0.0, record.model_id),
                    )
                    records.append(
                        BenchmarkTrial(
                            scenario_id=task.task_id,
                            policy_id=policy,
                            success=selected.success,
                            score=selected.score
                            if selected.score is not None
                            else float(selected.success),
                            cost_usd=selected.cost_usd,
                            latency_ms=selected.latency_ms,
                            selected_model_id=selected_id,
                            oracle_model_id=oracle.model_id,
                            task_family=task_family,
                            pair_id=f"rep-{repetition}",
                            predicted_success=selected.predicted_success,
                            metadata={
                                "data_kind": "synthetic_trial",
                                "split": "evaluation",
                                "seed": SEED,
                                "scenario_index": scenario_index,
                                "repetition": repetition,
                                "difficulty": task.metadata["difficulty"],
                                "routing_score": selected_rationale.total_score,
                            },
                        )
                    )
    return records


def _jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        _canonical_json(_round_tree(record)) + b"\n" for record in records
    )


def _calibration_metrics(
    policy_id: str,
    trials: list[BenchmarkTrial],
) -> tuple[float | None, float | None]:
    records = [
        TrialRecord(
            model_id=policy_id,
            success=trial.success,
            latency_ms=trial.latency_ms,
            cost_usd=trial.cost_usd,
            score=trial.score,
            predicted_success=trial.predicted_success,
            task_id=trial.scenario_id,
            task_family=trial.task_family,
        )
        for trial in trials
        if trial.policy_id == policy_id
    ]
    profile = aggregate_calibration(records)[policy_id]
    return profile.brier_score, profile.expected_calibration_error


def _recommend_policy(policies: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the generated fixture's explicit operating-envelope rule."""

    eligibility = RECOMMENDATION_RULE["eligibility"]
    eligible = [
        policy
        for policy in policies
        if policy["mean_cost_usd"] <= eligibility["mean_cost_usd_lte"]
        and policy["p95_latency_ms"] <= eligibility["p95_latency_ms_lte"]
    ]
    if not eligible:
        raise RuntimeError("recommendation rule has no eligible policy")
    selected = min(
        eligible,
        key=lambda policy: (
            -policy["success_rate"],
            -policy["mean_score"],
            policy["policy_id"],
        ),
    )
    selected["recommended"] = True
    return {
        **RECOMMENDATION_RULE,
        "eligible_policy_ids": sorted(
            policy["policy_id"] for policy in eligible
        ),
        "selected_policy_id": selected["policy_id"],
    }


def _comparison_conclusion(
    ci_low: float | None,
    ci_high: float | None,
) -> str:
    if ci_low is None or ci_high is None:
        return "insufficient_scenarios"
    if ci_low > 0.0:
        return "higher"
    if ci_high < 0.0:
        return "lower"
    return "inconclusive"


def _fixture(
    calibration_trials: list[TrialRecord],
    evaluation_outcomes: list[TrialRecord],
    policy_trials: list[BenchmarkTrial],
    candidates: list[ModelCandidate],
    *,
    calibration_corpus: bytes,
    evaluation_corpus: bytes,
    policy_corpus: bytes,
) -> dict[str, Any]:
    benchmark = AgenticBenchmark(policy_trials)
    report = benchmark.report(
        baseline_policy_id="balanced",
        bootstrap_samples=BOOTSTRAP_SAMPLES,
        seed=SEED,
        confidence_level=CONFIDENCE_LEVEL,
        missing_pair_behavior="error",
    )
    policies = []
    for policy_id in POLICIES:
        summary = report.summaries[policy_id]
        brier, calibration_error = _calibration_metrics(
            policy_id, policy_trials
        )
        policies.append(
            {
                "policy_id": policy_id,
                "trials": summary.trial_count,
                "scenarios": summary.scenario_count,
                "repetitions_per_scenario": REPETITIONS,
                "success_rate": summary.success_rate,
                "success_ci": [
                    summary.success_ci_low,
                    summary.success_ci_high,
                ],
                "trial_success_rate": summary.trial_success_rate,
                "mean_score": summary.mean_score,
                "p50_latency_ms": summary.p50_latency_ms,
                "p95_latency_ms": summary.p95_latency_ms,
                "mean_cost_usd": summary.mean_cost_usd,
                "brier_score": brier,
                "expected_calibration_error": calibration_error,
                "interval_method": summary.interval_method,
                "interval_status": summary.interval_status,
                "confidence_level": summary.confidence_level,
            }
        )
    recommendation = _recommend_policy(policies)

    deltas = {
        (delta.contender_policy_id, delta.metric): delta
        for delta in report.paired_deltas
    }
    comparison_specs = (
        ("quality", "success", "success_rate"),
        ("economy", "cost_usd", "mean_cost_usd"),
        ("deadline", "latency_ms", "mean_latency_ms"),
    )
    comparisons = []
    for challenger, raw_metric, public_metric in comparison_specs:
        delta = deltas[(challenger, raw_metric)]
        comparisons.append(
            {
                "baseline": "balanced",
                "challenger": challenger,
                "metric": public_metric,
                "delta": delta.mean_delta,
                "confidence_interval": [delta.ci_low, delta.ci_high],
                "conclusion": _comparison_conclusion(
                    delta.ci_low,
                    delta.ci_high,
                ),
                "interval_method": (
                    "paired_scenario_cluster_bootstrap_percentile"
                ),
                "interval_status": delta.interval_status,
                "matched_pairs": delta.matched_pair_count,
                "matched_scenarios": delta.scenario_count,
                "unmatched_baseline": delta.baseline_unmatched_count,
                "unmatched_challenger": delta.contender_unmatched_count,
            }
        )

    counts: dict[tuple[str, str, str], int] = {}
    totals: dict[tuple[str, str], int] = {}
    for trial in policy_trials:
        if trial.selected_model_id is None:
            continue
        key = (trial.policy_id, trial.task_family)
        totals[key] = totals.get(key, 0) + 1
        count_key = (*key, trial.selected_model_id)
        counts[count_key] = counts.get(count_key, 0) + 1
    routing_matrix = [
        {
            "policy_id": policy_id,
            "task_class": task_family,
            "model_id": model["model_id"],
            "share": counts.get(
                (policy_id, task_family, model["model_id"]), 0
            )
            / totals[(policy_id, task_family)],
        }
        for policy_id in POLICIES
        for task_family in TASK_FAMILIES
        for model in MODEL_CONFIG
    ]

    config_bytes = _canonical_json(SIMULATION_CONFIG)
    code_paths = (
        Path("scripts/generate_routing_fixture.py"),
        Path("src/charon/evaluation/agentic_benchmark.py"),
        Path("src/charon/routing/calibration.py"),
        Path("src/charon/routing/policy.py"),
    )
    code_hashes = {
        path.as_posix(): _sha256((REPO_ROOT / path).read_bytes())
        for path in code_paths
    }
    return _round_tree(
        {
            "schema_version": "1.0",
            "dataset_id": "routing-fixture-v2",
            "data_kind": "generated_fixture",
            "seed": SEED,
            "description": (
                "Deterministic synthetic demonstration data for the graph "
                "control plane. These values are not live provider measurements."
            ),
            "task_classes": list(TASK_FAMILIES),
            "models": [candidate.to_dict() for candidate in candidates],
            "policies": policies,
            "recommendation": recommendation,
            "comparisons": comparisons,
            "routing_matrix": routing_matrix,
            "provenance": {
                "generation_method": "seeded_synthetic_simulation",
                "generator_version": GENERATOR_VERSION,
                "generator_path": "scripts/generate_routing_fixture.py",
                "config_sha256": _sha256(config_bytes),
                "code_sha256": code_hashes,
                "raw_corpora": {
                    "model_calibration_trials": {
                        "path": f"examples/routing/{MODEL_CORPUS_NAME}",
                        "split": "calibration",
                        "records": len(calibration_trials),
                        "scenarios": len(TASK_FAMILIES)
                        * SCENARIOS_PER_FAMILY,
                        "sha256": _sha256(calibration_corpus),
                    },
                    "evaluation_model_outcomes": {
                        "path": (
                            f"examples/routing/{EVALUATION_OUTCOMES_NAME}"
                        ),
                        "split": "evaluation",
                        "records": len(evaluation_outcomes),
                        "scenarios": len(TASK_FAMILIES)
                        * SCENARIOS_PER_FAMILY,
                        "sha256": _sha256(evaluation_corpus),
                    },
                    "policy_benchmark_trials": {
                        "path": f"examples/routing/{POLICY_CORPUS_NAME}",
                        "split": "evaluation",
                        "records": len(policy_trials),
                        "scenarios": len(TASK_FAMILIES)
                        * SCENARIOS_PER_FAMILY,
                        "sha256": _sha256(policy_corpus),
                    },
                },
                "calibration_scenario_count": len(TASK_FAMILIES)
                * SCENARIOS_PER_FAMILY,
                "evaluation_scenario_count": len(TASK_FAMILIES)
                * SCENARIOS_PER_FAMILY,
                "repetitions_per_scenario": REPETITIONS,
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "confidence_level": CONFIDENCE_LEVEL,
            },
        }
    )


def generate_artifacts() -> dict[str, bytes]:
    """Return every generated artifact as stable bytes keyed by file name."""

    calibration_trials = generate_model_trials(split="calibration")
    evaluation_outcomes = generate_model_trials(split="evaluation")
    candidates = _build_candidates(calibration_trials)
    policy_trials = generate_policy_trials(evaluation_outcomes, candidates)
    calibration_corpus = _jsonl(
        record.to_dict() for record in calibration_trials
    )
    evaluation_corpus = _jsonl(
        record.to_dict() for record in evaluation_outcomes
    )
    policy_corpus = _jsonl(record.to_dict() for record in policy_trials)
    fixture = _fixture(
        calibration_trials,
        evaluation_outcomes,
        policy_trials,
        candidates,
        calibration_corpus=calibration_corpus,
        evaluation_corpus=evaluation_corpus,
        policy_corpus=policy_corpus,
    )
    fixture_bytes = (
        json.dumps(
            fixture,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    return {
        MODEL_CORPUS_NAME: calibration_corpus,
        EVALUATION_OUTCOMES_NAME: evaluation_corpus,
        POLICY_CORPUS_NAME: policy_corpus,
        FIXTURE_NAME: fixture_bytes,
    }


def _check(output_dir: Path, artifacts: dict[str, bytes]) -> int:
    stale: list[Path] = []
    for name, expected in artifacts.items():
        path = output_dir / name
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            actual = b""
        if actual != expected:
            stale.append(path)
    if stale:
        for path in stale:
            print(f"out of date: {path}", file=sys.stderr)
        return 1
    print("routing fixture and raw corpora are reproducible and up to date")
    return 0


def _write(output_dir: Path, artifacts: dict[str, bytes]) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        path = output_dir / name
        path.write_bytes(content)
        print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate deterministic synthetic routing calibration data."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that checked-in artifacts match generated bytes",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "examples" / "routing",
        help="directory for the fixture and raw JSONL corpora",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifacts = generate_artifacts()
    output_dir = args.output_dir.resolve()
    if args.check:
        return _check(output_dir, artifacts)
    return _write(output_dir, artifacts)


if __name__ == "__main__":
    raise SystemExit(main())
