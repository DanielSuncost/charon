from __future__ import annotations

import json

import pytest

from charon.routing import (
    CalibrationProfile,
    ModelCandidate,
    MultiObjectiveRouter,
    PolicyName,
    TaskProfile,
    TrialRecord,
    aggregate_calibration,
)


def _candidate(
    candidate_id: str,
    *,
    capabilities: tuple[str, ...] = ("tools",),
    context_window: int = 1_000,
    cost: float = 0.2,
    latency: float = 500.0,
    quality: float = 0.9,
    reliability: float = 0.9,
    calibration: CalibrationProfile | None = None,
) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider="test",
        model_id=candidate_id,
        capabilities=capabilities,
        context_window=context_window,
        estimated_cost_usd=cost,
        estimated_latency_ms=latency,
        estimated_quality=quality,
        estimated_reliability=reliability,
        calibration=calibration,
    )


def _profile(
    model_id: str,
    *,
    trials: int,
    successes: int,
    ci_low: float,
) -> CalibrationProfile:
    return CalibrationProfile(
        model_id=model_id,
        trial_count=trials,
        success_count=successes,
        success_rate=successes / trials,
        success_ci_low=ci_low,
        success_ci_high=0.98,
        p50_latency_ms=400.0,
        p95_latency_ms=500.0,
        mean_cost_usd=0.2,
        mean_score=0.9,
        brier_score=None,
        expected_calibration_error=None,
    )


def test_router_enforces_every_hard_constraint_and_explains_rejections() -> None:
    task = TaskProfile(
        task_id="task-1",
        required_capabilities=("tools",),
        context_tokens=100,
        max_cost_usd=0.5,
        quality_floor=0.5,
        latency_slo_ms=1_000,
    )
    candidates = [
        _candidate("missing", capabilities=("text",)),
        _candidate("context", context_window=50),
        _candidate("budget", cost=0.8),
        _candidate("quality", quality=0.6),
        _candidate("latency", latency=1_500),
        _candidate("valid"),
    ]

    decision = MultiObjectiveRouter().route(task, candidates)

    assert decision.selected_candidate_id == "valid"
    rejected = {
        rationale.candidate_id: {
            violation.constraint for violation in rationale.violations
        }
        for rationale in decision.candidates
        if not rationale.feasible
    }
    assert rejected == {
        "missing": {"capabilities"},
        "context": {"context_window"},
        "budget": {"budget"},
        "quality": {"quality_floor"},
        "latency": {"latency_slo"},
    }
    assert json.loads(json.dumps(decision.to_dict()))["feasible"] is True


def test_policy_profiles_make_distinct_explainable_tradeoffs() -> None:
    candidates = [
        _candidate(
            "high-quality",
            cost=0.9,
            latency=900,
            quality=0.98,
            reliability=0.98,
        ),
        _candidate(
            "low-cost",
            cost=0.05,
            latency=400,
            quality=0.75,
            reliability=0.85,
        ),
        _candidate(
            "low-latency",
            cost=0.3,
            latency=100,
            quality=0.75,
            reliability=0.85,
        ),
    ]
    router = MultiObjectiveRouter()
    task = TaskProfile(task_id="tradeoff")

    assert (
        router.route(task, candidates, policy=PolicyName.QUALITY)
        .selected_candidate_id
        == "high-quality"
    )
    assert (
        router.route(task, candidates, policy=PolicyName.ECONOMY)
        .selected_candidate_id
        == "low-cost"
    )
    assert (
        router.route(task, candidates, policy=PolicyName.DEADLINE)
        .selected_candidate_id
        == "low-latency"
    )


def test_router_is_deterministic_and_breaks_exact_ties_by_candidate_id() -> None:
    task = TaskProfile(task_id="tie")
    left = _candidate("a")
    right = _candidate("b")
    router = MultiObjectiveRouter(
        policy_weights={
            PolicyName.BALANCED: {
                "quality": 1,
                "reliability": 1,
                "cost": 1,
                "latency": 1,
            }
        }
    )

    forward = router.route(task, [right, left])
    reverse = router.route(task, [left, right])

    assert forward.to_dict() == reverse.to_dict()
    assert forward.selected_candidate_id == "a"


def test_calibration_support_reduces_uncertainty_and_changes_ranking() -> None:
    low_support = _profile(
        "low-support", trials=10, successes=9, ci_low=0.60
    )
    high_support = _profile(
        "high-support", trials=100, successes=90, ci_low=0.82
    )
    decision = MultiObjectiveRouter().route(
        TaskProfile(task_id="support"),
        [
            _candidate("low-support", calibration=low_support),
            _candidate("high-support", calibration=high_support),
        ],
        policy=PolicyName.QUALITY,
    )

    assert decision.selected_candidate_id == "high-support"
    estimates = {
        rationale.candidate_id: rationale.estimate
        for rationale in decision.candidates
    }
    assert (
        estimates["high-support"].uncertainty_width
        < estimates["low-support"].uncertainty_width
    )
    assert (
        estimates["high-support"].quality_lower_bound
        > estimates["low-support"].quality_lower_bound
    )


def test_calibration_aggregation_reports_accuracy_latency_and_calibration() -> None:
    records = [
        TrialRecord(
            "m",
            True,
            latency_ms=100,
            cost_usd=0.1,
            score=1.0,
            predicted_success=0.9,
        ),
        TrialRecord(
            "m",
            False,
            latency_ms=200,
            cost_usd=0.2,
            score=0.2,
            predicted_success=0.6,
        ),
        TrialRecord(
            "m",
            True,
            latency_ms=300,
            cost_usd=0.3,
            score=0.8,
            predicted_success=0.7,
        ),
        TrialRecord(
            "m",
            False,
            latency_ms=400,
            cost_usd=0.4,
            score=0.0,
            predicted_success=0.1,
        ),
    ]

    profile = aggregate_calibration(records, ece_bins=2)["m"]

    assert profile.trial_count == 4
    assert profile.success_count == 2
    assert profile.success_rate == pytest.approx(0.5)
    assert profile.success_ci_low < 0.5 < profile.success_ci_high
    assert profile.p50_latency_ms == pytest.approx(250)
    assert profile.p95_latency_ms == pytest.approx(385)
    assert profile.mean_cost_usd == pytest.approx(0.25)
    assert profile.mean_score == pytest.approx(0.5)
    assert profile.brier_score == pytest.approx(0.1175)
    assert profile.expected_calibration_error == pytest.approx(0.075)
    assert profile.probability_count == 4
    assert profile.score_count == 4


@pytest.mark.parametrize("success", ["false", "true", 0, 1, None])
def test_trial_record_requires_a_json_boolean(success: object) -> None:
    payload = {
        "model_id": "m",
        "success": success,
        "latency_ms": 10,
        "cost_usd": 0.01,
    }

    with pytest.raises(ValueError, match="success must be a boolean"):
        TrialRecord.from_dict(payload)

    with pytest.raises(ValueError, match="success must be a boolean"):
        TrialRecord(
            model_id="m",
            success=success,  # type: ignore[arg-type]
            latency_ms=10,
            cost_usd=0.01,
        )


def test_trial_record_does_not_invent_a_missing_outcome() -> None:
    with pytest.raises(ValueError, match="success is required"):
        TrialRecord.from_dict(
            {"model_id": "m", "latency_ms": 10, "cost_usd": 0.01}
        )


def _valid_calibration_values() -> dict[str, object]:
    return {
        "model_id": "m",
        "trial_count": 10,
        "success_count": 8,
        "success_rate": 0.8,
        "success_ci_low": 0.5,
        "success_ci_high": 0.95,
        "p50_latency_ms": 100,
        "p95_latency_ms": 200,
        "mean_cost_usd": 0.1,
        "mean_score": 0.8,
        "brier_score": 0.1,
        "expected_calibration_error": 0.05,
        "probability_count": 10,
        "score_count": 10,
        "prior_alpha": 1.0,
        "prior_beta": 1.0,
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"trial_count": True}, "trial_count"),
        ({"success_count": 11}, "success_count cannot exceed"),
        ({"probability_count": 11}, "probability_count cannot exceed"),
        ({"score_count": 11}, "score_count cannot exceed"),
        (
            {"success_ci_low": 0.9, "success_ci_high": 0.8},
            "success_ci_low cannot exceed",
        ),
        (
            {"p50_latency_ms": 201, "p95_latency_ms": 200},
            "p50_latency_ms cannot exceed",
        ),
        ({"mean_cost_usd": -0.1}, "mean_cost_usd"),
        ({"mean_score": float("nan")}, "mean_score"),
        ({"brier_score": 1.1}, "brier_score"),
        ({"expected_calibration_error": -0.1}, "expected_calibration_error"),
        ({"prior_alpha": float("inf")}, "prior_alpha"),
        ({"prior_beta": 0}, "prior_beta"),
    ],
)
def test_calibration_profile_rejects_inconsistent_or_nonfinite_data(
    updates: dict[str, object],
    message: str,
) -> None:
    values = {**_valid_calibration_values(), **updates}

    with pytest.raises(ValueError, match=message):
        CalibrationProfile(**values)  # type: ignore[arg-type]


def test_calibration_profile_from_dict_does_not_coerce_boolean_counts() -> None:
    values = {**_valid_calibration_values(), "trial_count": True}

    with pytest.raises(ValueError, match="trial_count"):
        CalibrationProfile.from_dict(values)


@pytest.mark.parametrize(
    "weights",
    [
        {"quality": -1.0},
        {"quality": float("nan")},
        {"quality": float("inf")},
        {"quality": True},
        {"quality": 0.0, "reliability": 0.0, "cost": 0.0, "latency": 0.0},
        {"quality": 1.0, "typo": 1.0},
    ],
)
def test_custom_policy_weights_must_be_known_finite_and_nonnegative(
    weights: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        MultiObjectiveRouter(
            policy_weights={PolicyName.BALANCED: weights}  # type: ignore[dict-item]
        )


def test_decision_serializes_exact_policy_profile_for_replay():
    decision = MultiObjectiveRouter().route(
        TaskProfile(task_id="replay-test"),
        [_candidate("only", quality=0.8, reliability=0.9)],
        policy=PolicyName.BALANCED,
    ).to_dict()

    assert decision["policy_version"] == "multi-objective-v1"
    assert decision["policy_weights"] == {
        "cost": 0.2,
        "latency": 0.2,
        "quality": 0.35,
        "reliability": 0.25,
    }
    assert sum(decision["policy_weights"].values()) == pytest.approx(1.0)
