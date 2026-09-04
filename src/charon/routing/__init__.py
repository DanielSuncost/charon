"""Model-routing policies and empirical calibration profiles."""

from charon.routing.calibration import (
    CalibrationProfile,
    TrialRecord,
    aggregate_calibration,
    aggregate_calibration_by_task,
    brier_score,
    expected_calibration_error,
    percentile,
    wilson_interval,
)
from charon.routing.policy import (
    CandidateEstimate,
    CandidateRationale,
    ConstraintViolation,
    ModelCandidate,
    MultiObjectiveRouter,
    POLICY_WEIGHTS,
    PolicyName,
    ROUTING_POLICY_VERSION,
    RoutingDecision,
    TaskProfile,
    route_models,
)

__all__ = [
    "CalibrationProfile",
    "CandidateEstimate",
    "CandidateRationale",
    "ConstraintViolation",
    "ModelCandidate",
    "MultiObjectiveRouter",
    "POLICY_WEIGHTS",
    "PolicyName",
    "ROUTING_POLICY_VERSION",
    "RoutingDecision",
    "TaskProfile",
    "TrialRecord",
    "aggregate_calibration",
    "aggregate_calibration_by_task",
    "brier_score",
    "expected_calibration_error",
    "percentile",
    "route_models",
    "wilson_interval",
]
