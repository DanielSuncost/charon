from charon.routing import (
    ModelCandidate,
    TaskProfile,
    TrialRecord,
    aggregate_calibration_by_task,
    route_models,
)


def _records():
    rows = []
    for model_id, family, successes, score in (
        ("model-a", "implementation", 8, 0.88),
        ("model-a", "verification", 4, 0.62),
        ("model-b", "implementation", 5, 0.69),
        ("model-b", "verification", 9, 0.93),
    ):
        for index in range(10):
            rows.append(TrialRecord(
                model_id=model_id,
                task_family=family,
                task_id=f"{family}-{index}",
                success=index < successes,
                score=score,
                latency_ms=1000 + index,
                cost_usd=0.01,
                predicted_success=score,
            ))
    return rows


def test_task_calibration_changes_route_by_task_family():
    grid = aggregate_calibration_by_task(_records())
    candidates = [
        ModelCandidate(
            candidate_id=model_id,
            provider="fixture",
            model_id=model_id,
            capabilities=("code",),
            context_window=100_000,
            estimated_cost_usd=0.01,
            estimated_latency_ms=1000,
            estimated_quality=0.5,
            task_calibrations=grid[model_id],
        )
        for model_id in ("model-a", "model-b")
    ]

    implementation = route_models(
        TaskProfile(
            task_id="impl",
            task_family="implementation",
            required_capabilities=("code",),
        ),
        candidates,
        policy="quality",
    )
    verification = route_models(
        TaskProfile(
            task_id="verify",
            task_family="verification",
            required_capabilities=("code",),
        ),
        candidates,
        policy="quality",
    )

    assert implementation.selected_candidate_id == "model-a"
    assert verification.selected_candidate_id == "model-b"


def test_task_profile_reads_class_and_complexity_aliases():
    task = TaskProfile.from_dict({
        "task_id": "task-1",
        "task_class": "planning",
        "complexity": 0.8,
    })

    assert task.task_family == "planning"
    assert task.complexity == 0.8

