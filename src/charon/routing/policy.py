"""Explainable, constraint-aware model routing.

Routing is a deterministic decision over explicit task requirements, model
capabilities, and optional calibration profiles.  Every candidate receives a
complete rationale, including hard-constraint failures and weighted utility
components, so a route can be audited or replayed offline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from charon.routing.calibration import CalibrationProfile


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _finite_non_negative(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric, not boolean")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative value")
    return number


def _optional_probability(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric, not boolean")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be a finite value in [0, 1]")
    return number


class PolicyName(str, Enum):
    BALANCED = "balanced"
    QUALITY = "quality"
    ECONOMY = "economy"
    DEADLINE = "deadline"


POLICY_WEIGHTS: dict[PolicyName, dict[str, float]] = {
    PolicyName.BALANCED: {
        "quality": 0.35,
        "reliability": 0.25,
        "cost": 0.20,
        "latency": 0.20,
    },
    PolicyName.QUALITY: {
        "quality": 0.65,
        "reliability": 0.30,
        "cost": 0.025,
        "latency": 0.025,
    },
    PolicyName.ECONOMY: {
        "quality": 0.20,
        "reliability": 0.20,
        "cost": 0.50,
        "latency": 0.10,
    },
    PolicyName.DEADLINE: {
        "quality": 0.20,
        "reliability": 0.20,
        "cost": 0.10,
        "latency": 0.50,
    },
}

ROUTING_POLICY_VERSION = "multi-objective-v1"


@dataclass(frozen=True)
class TaskProfile:
    """Typed requirements for one routed task."""

    task_id: str
    task_family: str = "default"
    complexity: float = 0.5
    required_capabilities: tuple[str, ...] = ()
    context_tokens: int = 0
    max_cost_usd: float | None = None
    quality_floor: float | None = None
    latency_slo_ms: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id is required")
        if not self.task_family.strip():
            raise ValueError("task_family is required")
        if not math.isfinite(float(self.complexity)) or not 0.0 <= float(self.complexity) <= 1.0:
            raise ValueError("complexity must be a finite value in [0, 1]")
        object.__setattr__(self, "complexity", float(self.complexity))
        if self.context_tokens < 0:
            raise ValueError("context_tokens must be non-negative")
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(sorted({
                str(capability).strip()
                for capability in self.required_capabilities
                if str(capability).strip()
            })),
        )
        if self.max_cost_usd is not None:
            object.__setattr__(
                self, "max_cost_usd",
                _finite_non_negative(
                    self.max_cost_usd, field_name="max_cost_usd",
                ),
            )
        if self.latency_slo_ms is not None:
            object.__setattr__(
                self, "latency_slo_ms",
                _finite_non_negative(
                    self.latency_slo_ms, field_name="latency_slo_ms",
                ),
            )
        if self.quality_floor is not None:
            object.__setattr__(
                self, "quality_floor",
                _optional_probability(
                    self.quality_floor, field_name="quality_floor",
                ),
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskProfile":
        return cls(
            task_id=str(value.get("task_id") or value.get("id") or ""),
            task_family=str(
                value.get("task_family")
                or value.get("task_class")
                or (value.get("metadata") or {}).get("task_family")
                or (value.get("metadata") or {}).get("task_class")
                or "default"
            ),
            complexity=float(
                value.get(
                    "complexity",
                    (value.get("metadata") or {}).get("complexity", 0.5),
                )
            ),
            required_capabilities=tuple(
                value.get("required_capabilities")
                or value.get("capabilities")
                or ()
            ),
            context_tokens=int(value.get("context_tokens", 0)),
            max_cost_usd=(
                value.get("max_cost_usd", value.get("budget_usd"))
            ),
            quality_floor=value.get("quality_floor"),
            latency_slo_ms=value.get(
                "latency_slo_ms", value.get("deadline_ms"),
            ),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_family": self.task_family,
            "complexity": self.complexity,
            "required_capabilities": list(self.required_capabilities),
            "context_tokens": self.context_tokens,
            "max_cost_usd": self.max_cost_usd,
            "quality_floor": self.quality_floor,
            "latency_slo_ms": self.latency_slo_ms,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ModelCandidate:
    """A routable model endpoint and its uncalibrated estimates."""

    candidate_id: str
    provider: str
    model_id: str
    capabilities: tuple[str, ...]
    context_window: int
    estimated_cost_usd: float
    estimated_latency_ms: float
    estimated_quality: float
    estimated_reliability: float = 0.9
    calibration: CalibrationProfile | None = None
    task_calibrations: Mapping[str, CalibrationProfile] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id is required")
        if not self.model_id.strip():
            raise ValueError("model_id is required")
        if self.context_window < 0:
            raise ValueError("context_window must be non-negative")
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted({
                str(capability).strip()
                for capability in self.capabilities
                if str(capability).strip()
            })),
        )
        for name in ("estimated_cost_usd", "estimated_latency_ms"):
            object.__setattr__(
                self, name,
                _finite_non_negative(getattr(self, name), field_name=name),
            )
        for name in ("estimated_quality", "estimated_reliability"):
            object.__setattr__(
                self, name,
                _optional_probability(getattr(self, name), field_name=name),
            )
        if self.calibration and self.calibration.model_id not in (
            self.model_id, self.candidate_id,
        ):
            raise ValueError(
                "calibration model_id must match model_id or candidate_id",
            )
        normalized_calibrations: dict[str, CalibrationProfile] = {}
        for task_family, profile in self.task_calibrations.items():
            family = str(task_family).strip()
            if not family:
                raise ValueError("task calibration family names must be non-empty")
            if not isinstance(profile, CalibrationProfile):
                profile = CalibrationProfile.from_dict(profile)
            if profile.model_id not in (self.model_id, self.candidate_id):
                raise ValueError(
                    "task calibration model_id must match model_id or candidate_id",
                )
            normalized_calibrations[family] = profile
        object.__setattr__(self, "task_calibrations", normalized_calibrations)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelCandidate":
        calibration = value.get("calibration")
        if calibration is not None and not isinstance(
            calibration, CalibrationProfile,
        ):
            calibration = CalibrationProfile.from_dict(calibration)
        task_calibrations = {
            str(task_family): (
                profile
                if isinstance(profile, CalibrationProfile)
                else CalibrationProfile.from_dict(profile)
            )
            for task_family, profile in (
                value.get("task_calibrations")
                or value.get("calibrations_by_task")
                or {}
            ).items()
        }
        model_id = str(value.get("model_id") or value.get("model") or "")
        return cls(
            candidate_id=str(
                value.get("candidate_id") or value.get("id") or model_id,
            ),
            provider=str(value.get("provider") or ""),
            model_id=model_id,
            capabilities=tuple(value.get("capabilities") or ()),
            context_window=int(value.get("context_window", 0)),
            estimated_cost_usd=float(value.get(
                "estimated_cost_usd", value.get("cost_usd", 0.0),
            )),
            estimated_latency_ms=float(value.get(
                "estimated_latency_ms", value.get("latency_ms", 0.0),
            )),
            estimated_quality=float(value.get(
                "estimated_quality", value.get("quality", 0.0),
            )),
            estimated_reliability=float(value.get(
                "estimated_reliability", value.get("reliability", 0.9),
            )),
            calibration=calibration,
            task_calibrations=task_calibrations,
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_latency_ms": self.estimated_latency_ms,
            "estimated_quality": self.estimated_quality,
            "estimated_reliability": self.estimated_reliability,
            "calibration": (
                self.calibration.to_dict() if self.calibration else None
            ),
            "task_calibrations": {
                task_family: profile.to_dict()
                for task_family, profile in sorted(self.task_calibrations.items())
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CandidateEstimate:
    """Effective metrics after applying calibration and uncertainty."""

    quality: float
    quality_lower_bound: float
    reliability: float
    reliability_lower_bound: float
    cost_usd: float
    expected_latency_ms: float
    slo_latency_ms: float
    support: int
    uncertainty_width: float
    calibration_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "quality_lower_bound": self.quality_lower_bound,
            "reliability": self.reliability,
            "reliability_lower_bound": self.reliability_lower_bound,
            "cost_usd": self.cost_usd,
            "expected_latency_ms": self.expected_latency_ms,
            "slo_latency_ms": self.slo_latency_ms,
            "support": self.support,
            "uncertainty_width": self.uncertainty_width,
            "calibration_used": self.calibration_used,
        }


@dataclass(frozen=True)
class ConstraintViolation:
    constraint: str
    required: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint": self.constraint,
            "required": self.required,
            "actual": self.actual,
            "message": self.message,
        }


@dataclass
class CandidateRationale:
    candidate_id: str
    feasible: bool
    estimate: CandidateEstimate
    violations: list[ConstraintViolation] = field(default_factory=list)
    normalized_scores: dict[str, float] = field(default_factory=dict)
    weighted_contributions: dict[str, float] = field(default_factory=dict)
    total_score: float | None = None
    rank: int | None = None
    selected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "feasible": self.feasible,
            "estimate": self.estimate.to_dict(),
            "violations": [
                violation.to_dict() for violation in self.violations
            ],
            "normalized_scores": dict(self.normalized_scores),
            "weighted_contributions": dict(self.weighted_contributions),
            "total_score": self.total_score,
            "rank": self.rank,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class RoutingDecision:
    task_id: str
    policy: str
    selected_candidate_id: str | None
    selection_reason: str
    candidates: tuple[CandidateRationale, ...]
    policy_weights: Mapping[str, float] = field(default_factory=dict)
    policy_version: str = ROUTING_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version is required")
        normalized = {
            str(name): _finite_non_negative(
                weight,
                field_name=f"{self.policy} policy weight {name}",
            )
            for name, weight in self.policy_weights.items()
        }
        total = sum(normalized.values())
        if normalized and not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError("policy_weights must sum to 1")
        object.__setattr__(
            self,
            "policy_weights",
            dict(sorted(normalized.items())),
        )

    @property
    def feasible(self) -> bool:
        return self.selected_candidate_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "policy": self.policy,
            "policy_version": self.policy_version,
            "policy_weights": dict(self.policy_weights),
            "selected_candidate_id": self.selected_candidate_id,
            "feasible": self.feasible,
            "selection_reason": self.selection_reason,
            "candidates": [
                rationale.to_dict() for rationale in self.candidates
            ],
        }


def _effective_estimate(
    candidate: ModelCandidate,
    task_family: str = "default",
) -> CandidateEstimate:
    calibration = (
        candidate.task_calibrations.get(task_family)
        or candidate.task_calibrations.get("*")
        or candidate.calibration
    )
    if calibration and calibration.trial_count > 0:
        reliability = _clamp01(calibration.success_rate)
        reliability_lower = _clamp01(calibration.success_ci_low)
        quality = _clamp01(
            calibration.mean_score
            if calibration.mean_score is not None
            else reliability
        )
        # Transfer the calibrated reliability uncertainty to the continuous
        # quality estimate.  This is conservative and shrinks automatically as
        # support grows.
        quality_lower = _clamp01(
            quality - max(0.0, reliability - reliability_lower),
        )
        return CandidateEstimate(
            quality=quality,
            quality_lower_bound=quality_lower,
            reliability=reliability,
            reliability_lower_bound=reliability_lower,
            cost_usd=calibration.mean_cost_usd,
            expected_latency_ms=calibration.p50_latency_ms,
            slo_latency_ms=calibration.p95_latency_ms,
            support=calibration.trial_count,
            uncertainty_width=calibration.uncertainty_width,
            calibration_used=True,
        )

    # Uncalibrated declarations remain usable, but carry an explicit uncertainty
    # reserve.  This prevents an unsupported optimistic estimate from outranking
    # an otherwise equivalent well-measured model.
    reserve = 0.25
    return CandidateEstimate(
        quality=candidate.estimated_quality,
        quality_lower_bound=_clamp01(
            candidate.estimated_quality - reserve,
        ),
        reliability=candidate.estimated_reliability,
        reliability_lower_bound=_clamp01(
            candidate.estimated_reliability - reserve,
        ),
        cost_usd=candidate.estimated_cost_usd,
        expected_latency_ms=candidate.estimated_latency_ms,
        slo_latency_ms=candidate.estimated_latency_ms,
        support=0,
        uncertainty_width=0.5,
        calibration_used=False,
    )


def _constraint_violations(
    task: TaskProfile,
    candidate: ModelCandidate,
    estimate: CandidateEstimate,
) -> list[ConstraintViolation]:
    violations: list[ConstraintViolation] = []
    available = set(candidate.capabilities)
    missing = (
        set(task.required_capabilities)
        if "*" not in available
        else set()
    ) - available
    if missing:
        violations.append(ConstraintViolation(
            constraint="capabilities",
            required=sorted(task.required_capabilities),
            actual=sorted(candidate.capabilities),
            message=f"missing capabilities: {', '.join(sorted(missing))}",
        ))
    if task.context_tokens > candidate.context_window:
        violations.append(ConstraintViolation(
            constraint="context_window",
            required=task.context_tokens,
            actual=candidate.context_window,
            message="task context exceeds the model context window",
        ))
    if (
        task.max_cost_usd is not None
        and estimate.cost_usd > task.max_cost_usd
    ):
        violations.append(ConstraintViolation(
            constraint="budget",
            required=task.max_cost_usd,
            actual=estimate.cost_usd,
            message="estimated cost exceeds the task budget",
        ))
    if (
        task.quality_floor is not None
        and estimate.quality_lower_bound < task.quality_floor
    ):
        violations.append(ConstraintViolation(
            constraint="quality_floor",
            required=task.quality_floor,
            actual=estimate.quality_lower_bound,
            message="uncertainty-adjusted quality is below the floor",
        ))
    if (
        task.latency_slo_ms is not None
        and estimate.slo_latency_ms > task.latency_slo_ms
    ):
        violations.append(ConstraintViolation(
            constraint="latency_slo",
            required=task.latency_slo_ms,
            actual=estimate.slo_latency_ms,
            message="calibrated p95 latency exceeds the SLO",
        ))
    return violations


def _lower_is_better(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low = min(values.values())
    high = max(values.values())
    if math.isclose(low, high):
        return {key: 1.0 for key in values}
    span = high - low
    return {
        key: _clamp01(1.0 - (value - low) / span)
        for key, value in values.items()
    }


class MultiObjectiveRouter:
    """Apply hard constraints, normalize utilities, and rank candidates."""

    def __init__(
        self,
        *,
        policy_weights: Mapping[
            PolicyName | str, Mapping[str, float]
        ] | None = None,
    ):
        if policy_weights is not None and not isinstance(
            policy_weights, Mapping
        ):
            raise ValueError("policy_weights must be an object")
        self._weights = {
            policy: dict(weights)
            for policy, weights in POLICY_WEIGHTS.items()
        }
        for raw_policy, weights in (policy_weights or {}).items():
            policy = (
                raw_policy
                if isinstance(raw_policy, PolicyName)
                else PolicyName(str(raw_policy))
            )
            if not isinstance(weights, Mapping):
                raise ValueError(
                    f"policy {policy.value} weights must be an object"
                )
            weight_keys = ("quality", "reliability", "cost", "latency")
            unknown = set(weights) - set(weight_keys)
            if unknown:
                raise ValueError(
                    f"policy {policy.value} has unknown weight(s): "
                    + ", ".join(sorted(str(key) for key in unknown))
                )
            clean = {
                key: _finite_non_negative(
                    weights.get(key, 0.0),
                    field_name=f"{policy.value}.{key} weight",
                )
                for key in weight_keys
            }
            total = sum(clean.values())
            if total <= 0.0:
                raise ValueError(f"policy {policy.value} has no positive weight")
            self._weights[policy] = {
                key: value / total for key, value in clean.items()
            }

    def route(
        self,
        task: TaskProfile | Mapping[str, Any],
        candidates: Iterable[ModelCandidate | Mapping[str, Any]],
        *,
        policy: PolicyName | str = PolicyName.BALANCED,
    ) -> RoutingDecision:
        task_profile = (
            task if isinstance(task, TaskProfile) else TaskProfile.from_dict(task)
        )
        policy_name = (
            policy if isinstance(policy, PolicyName)
            else PolicyName(str(policy))
        )
        models = [
            item if isinstance(item, ModelCandidate)
            else ModelCandidate.from_dict(item)
            for item in candidates
        ]
        ids = [model.candidate_id for model in models]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_id values must be unique")

        rationales: list[CandidateRationale] = []
        by_id: dict[str, CandidateRationale] = {}
        for candidate in sorted(models, key=lambda item: item.candidate_id):
            estimate = _effective_estimate(candidate, task_profile.task_family)
            violations = _constraint_violations(
                task_profile, candidate, estimate,
            )
            rationale = CandidateRationale(
                candidate_id=candidate.candidate_id,
                feasible=not violations,
                estimate=estimate,
                violations=violations,
            )
            rationales.append(rationale)
            by_id[candidate.candidate_id] = rationale

        feasible = [item for item in rationales if item.feasible]
        if feasible:
            cost_scores = _lower_is_better({
                item.candidate_id: item.estimate.cost_usd
                for item in feasible
            })
            latency_scores = _lower_is_better({
                item.candidate_id: item.estimate.slo_latency_ms
                for item in feasible
            })
            weights = self._weights[policy_name]
            for item in feasible:
                components = {
                    "quality": item.estimate.quality_lower_bound,
                    "reliability": item.estimate.reliability_lower_bound,
                    "cost": cost_scores[item.candidate_id],
                    "latency": latency_scores[item.candidate_id],
                }
                item.normalized_scores = {
                    key: round(value, 12)
                    for key, value in components.items()
                }
                item.weighted_contributions = {
                    key: round(components[key] * weights[key], 12)
                    for key in components
                }
                item.total_score = round(
                    sum(item.weighted_contributions.values()), 12,
                )

            ranked = sorted(
                feasible,
                key=lambda item: (
                    -(item.total_score if item.total_score is not None else -1.0),
                    item.candidate_id,
                ),
            )
            for index, item in enumerate(ranked, start=1):
                item.rank = index
            selected = ranked[0]
            selected.selected = True
            selected_id: str | None = selected.candidate_id
            selection_reason = (
                f"{selected_id} ranked first under the {policy_name.value} "
                f"policy with score {selected.total_score:.6f}"
            )
        else:
            selected_id = None
            selection_reason = (
                "no candidate satisfied all hard constraints"
                if rationales else "no candidates were supplied"
            )

        # Feasible candidates are presented in rank order; rejected candidates
        # follow in stable id order so serialized explanations are reproducible.
        ordered = sorted(
            rationales,
            key=lambda item: (
                0 if item.rank is not None else 1,
                item.rank if item.rank is not None else 0,
                item.candidate_id,
            ),
        )
        return RoutingDecision(
            task_id=task_profile.task_id,
            policy=policy_name.value,
            policy_version=ROUTING_POLICY_VERSION,
            policy_weights={
                key: round(value, 12)
                for key, value in self._weights[policy_name].items()
            },
            selected_candidate_id=selected_id,
            selection_reason=selection_reason,
            candidates=tuple(ordered),
        )


def route_models(
    task: TaskProfile | Mapping[str, Any],
    candidates: Iterable[ModelCandidate | Mapping[str, Any]],
    *,
    policy: PolicyName | str = PolicyName.BALANCED,
) -> RoutingDecision:
    """Convenience wrapper using the built-in policy profiles."""

    return MultiObjectiveRouter().route(task, candidates, policy=policy)


__all__ = [
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
    "route_models",
]
