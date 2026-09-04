"""Calibration summaries for model-routing decisions.

The module deliberately works on small, JSON-friendly records.  Raw trials can
be written as JSONL, aggregated offline, and loaded back into the router without
provider-specific objects or third-party statistics packages.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_Z_95 = 1.959963984540054


def _bounded_probability(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number, not a boolean")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be a finite value in [0, 1]")
    return number


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number, not a boolean")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _non_negative(value: Any, *, field_name: str) -> float:
    number = _finite_number(value, field_name=field_name)
    if number < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative value")
    return number


def _strict_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _non_negative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_finite(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number, not a boolean")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field_name} must be a finite positive value")
    return number


def _json_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} keys must be strings")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serializable: {exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError(f"{field_name} must be an object")
    return decoded


@dataclass(frozen=True)
class TrialRecord:
    """One scored model attempt used to build a calibration profile."""

    model_id: str
    success: bool
    latency_ms: float
    cost_usd: float
    score: float | None = None
    predicted_success: float | None = None
    task_id: str = ""
    task_family: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id is required")
        object.__setattr__(
            self, "success", _strict_bool(self.success, field_name="success")
        )
        for name in ("task_id", "task_family"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        object.__setattr__(
            self, "latency_ms",
            _non_negative(self.latency_ms, field_name="latency_ms"),
        )
        object.__setattr__(
            self, "cost_usd",
            _non_negative(self.cost_usd, field_name="cost_usd"),
        )
        if self.score is not None:
            object.__setattr__(
                self, "score",
                _bounded_probability(self.score, field_name="score"),
            )
        if self.predicted_success is not None:
            object.__setattr__(
                self, "predicted_success",
                _bounded_probability(
                    self.predicted_success, field_name="predicted_success",
                ),
            )
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, field_name="metadata"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TrialRecord":
        if not isinstance(value, Mapping):
            raise ValueError("TrialRecord must be an object")
        if "success" not in value and "passed" not in value:
            raise ValueError("success is required")
        success = (
            value["success"]
            if "success" in value
            else value["passed"]
        )
        return cls(
            model_id=str(value.get("model_id") or value.get("candidate_id") or ""),
            success=_strict_bool(success, field_name="success"),
            latency_ms=value.get("latency_ms", 0.0),
            cost_usd=value.get("cost_usd", 0.0),
            score=(
                None if value.get("score") is None
                else value["score"]
            ),
            predicted_success=(
                None
                if value.get("predicted_success",
                             value.get("predicted_probability")) is None
                else value.get(
                    "predicted_success", value.get("predicted_probability"),
                )
            ),
            task_id=str(value.get("task_id") or value.get("scenario_id") or ""),
            task_family=str(value.get("task_family") or ""),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "score": self.score,
            "predicted_success": self.predicted_success,
            "task_id": self.task_id,
            "task_family": self.task_family,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CalibrationProfile:
    """Aggregated performance and uncertainty for one model."""

    model_id: str
    trial_count: int
    success_count: int
    success_rate: float
    success_ci_low: float
    success_ci_high: float
    p50_latency_ms: float
    p95_latency_ms: float
    mean_cost_usd: float
    mean_score: float | None
    brier_score: float | None
    expected_calibration_error: float | None
    probability_count: int = 0
    score_count: int = 0
    prior_alpha: float = 1.0
    prior_beta: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id is required")
        for name in (
            "trial_count",
            "success_count",
            "probability_count",
            "score_count",
        ):
            _non_negative_int(getattr(self, name), field_name=name)
        if self.success_count > self.trial_count:
            raise ValueError("success_count cannot exceed trial_count")
        if self.probability_count > self.trial_count:
            raise ValueError("probability_count cannot exceed trial_count")
        if self.score_count > self.trial_count:
            raise ValueError("score_count cannot exceed trial_count")
        for name in ("success_rate", "success_ci_low", "success_ci_high"):
            object.__setattr__(
                self,
                name,
                _bounded_probability(getattr(self, name), field_name=name),
            )
        if self.success_ci_low > self.success_ci_high:
            raise ValueError("success_ci_low cannot exceed success_ci_high")
        # ``success_rate`` is beta-smoothed while the interval is an
        # outcome-only Wilson interval. With an informative prior, the
        # posterior mean can legitimately fall outside that observed-data
        # interval, so containment is intentionally not an invariant.
        for name in ("p50_latency_ms", "p95_latency_ms", "mean_cost_usd"):
            object.__setattr__(
                self,
                name,
                _non_negative(getattr(self, name), field_name=name),
            )
        if self.p50_latency_ms > self.p95_latency_ms:
            raise ValueError("p50_latency_ms cannot exceed p95_latency_ms")
        for name in (
            "mean_score",
            "brier_score",
            "expected_calibration_error",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _bounded_probability(value, field_name=name),
                )
        for name in ("prior_alpha", "prior_beta"):
            object.__setattr__(
                self,
                name,
                _positive_finite(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, field_name="metadata"),
        )

    @property
    def uncertainty_width(self) -> float:
        return max(0.0, self.success_ci_high - self.success_ci_low)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CalibrationProfile":
        return cls(
            model_id=str(value.get("model_id") or value.get("candidate_id") or ""),
            trial_count=value.get("trial_count", value.get("trials", 0)),
            success_count=value.get(
                "success_count", value.get("successes", 0),
            ),
            success_rate=value.get("success_rate", 0.0),
            success_ci_low=value.get("success_ci_low", 0.0),
            success_ci_high=value.get("success_ci_high", 1.0),
            p50_latency_ms=value.get("p50_latency_ms", 0.0),
            p95_latency_ms=value.get("p95_latency_ms", 0.0),
            mean_cost_usd=value.get("mean_cost_usd", 0.0),
            mean_score=(
                None if value.get("mean_score") is None
                else value["mean_score"]
            ),
            brier_score=(
                None if value.get("brier_score") is None
                else value["brier_score"]
            ),
            expected_calibration_error=(
                None if value.get("expected_calibration_error",
                                  value.get("ece")) is None
                else value.get(
                    "expected_calibration_error", value.get("ece"),
                )
            ),
            probability_count=value.get("probability_count", 0),
            score_count=value.get("score_count", 0),
            prior_alpha=value.get("prior_alpha", 1.0),
            prior_beta=value.get("prior_beta", 1.0),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "trial_count": self.trial_count,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "success_ci_low": self.success_ci_low,
            "success_ci_high": self.success_ci_high,
            "uncertainty_width": self.uncertainty_width,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "mean_cost_usd": self.mean_cost_usd,
            "mean_score": self.mean_score,
            "brier_score": self.brier_score,
            "expected_calibration_error": self.expected_calibration_error,
            "probability_count": self.probability_count,
            "score_count": self.score_count,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
            "metadata": dict(self.metadata),
        }


def wilson_interval(
    successes: int,
    trials: int,
    *,
    z: float = _Z_95,
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""

    _non_negative_int(successes, field_name="successes")
    _non_negative_int(trials, field_name="trials")
    if successes > trials:
        raise ValueError("expected 0 <= successes <= trials")
    z = _positive_finite(z, field_name="z")
    if trials == 0:
        return (0.0, 1.0)
    p = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (p + z2 / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt((p * (1.0 - p) + z2 / (4.0 * trials)) / trials)
        / denominator
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def percentile(values: Iterable[float], q: float) -> float:
    """Linearly interpolated percentile using a deterministic rank rule."""

    q = _bounded_probability(q, field_name="q")
    ordered = sorted(
        _finite_number(value, field_name="percentile value") for value in values
    )
    if not ordered:
        raise ValueError("at least one value is required")
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def brier_score(records: Iterable[TrialRecord]) -> float | None:
    pairs = [
        (record.predicted_success, float(record.success))
        for record in records
        if record.predicted_success is not None
    ]
    if not pairs:
        return None
    return statistics.fmean((prediction - outcome) ** 2
                            for prediction, outcome in pairs)


def expected_calibration_error(
    records: Iterable[TrialRecord],
    *,
    bins: int = 10,
) -> float | None:
    """Equal-width expected calibration error for probabilistic trials."""

    if isinstance(bins, bool) or not isinstance(bins, int) or bins <= 0:
        raise ValueError("bins must be a positive integer")
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    total = 0
    for record in records:
        if record.predicted_success is None:
            continue
        index = min(int(record.predicted_success * bins), bins - 1)
        buckets[index].append(
            (record.predicted_success, float(record.success)),
        )
        total += 1
    if total == 0:
        return None
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = statistics.fmean(pair[0] for pair in bucket)
        accuracy = statistics.fmean(pair[1] for pair in bucket)
        error += (len(bucket) / total) * abs(confidence - accuracy)
    return error


def aggregate_calibration(
    records: Iterable[TrialRecord | Mapping[str, Any]],
    *,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    ece_bins: int = 10,
) -> dict[str, CalibrationProfile]:
    """Aggregate raw trials into one immutable profile per model.

    Success probability is beta-smoothed; the interval remains the standard
    Wilson interval over observed outcomes so its support is transparent.
    """

    prior_alpha = _positive_finite(prior_alpha, field_name="prior_alpha")
    prior_beta = _positive_finite(prior_beta, field_name="prior_beta")
    if isinstance(ece_bins, bool) or not isinstance(ece_bins, int) or ece_bins <= 0:
        raise ValueError("ece_bins must be a positive integer")
    grouped: dict[str, list[TrialRecord]] = {}
    for raw in records:
        record = raw if isinstance(raw, TrialRecord) else TrialRecord.from_dict(raw)
        grouped.setdefault(record.model_id, []).append(record)

    profiles: dict[str, CalibrationProfile] = {}
    for model_id in sorted(grouped):
        rows = grouped[model_id]
        trials = len(rows)
        successes = sum(int(row.success) for row in rows)
        posterior = (
            (successes + prior_alpha)
            / (trials + prior_alpha + prior_beta)
        )
        ci_low, ci_high = wilson_interval(successes, trials)
        scored = [row.score for row in rows if row.score is not None]
        probabilistic = [
            row for row in rows if row.predicted_success is not None
        ]
        profiles[model_id] = CalibrationProfile(
            model_id=model_id,
            trial_count=trials,
            success_count=successes,
            success_rate=posterior,
            success_ci_low=ci_low,
            success_ci_high=ci_high,
            p50_latency_ms=percentile(
                (row.latency_ms for row in rows), 0.50,
            ),
            p95_latency_ms=percentile(
                (row.latency_ms for row in rows), 0.95,
            ),
            mean_cost_usd=statistics.fmean(row.cost_usd for row in rows),
            mean_score=statistics.fmean(scored) if scored else None,
            brier_score=brier_score(rows),
            expected_calibration_error=expected_calibration_error(
                rows, bins=ece_bins,
            ),
            probability_count=len(probabilistic),
            score_count=len(scored),
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
        )
    return profiles


def aggregate_calibration_by_task(
    records: Iterable[TrialRecord | Mapping[str, Any]],
    *,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    ece_bins: int = 10,
) -> dict[str, dict[str, CalibrationProfile]]:
    """Build a model-by-task-family calibration grid.

    The outer key is ``model_id`` and the inner key is ``task_family``. Records
    without a family use ``"default"``. Keeping the grouping explicit prevents
    a model's strength on one task class from being treated as evidence for an
    unrelated class.
    """

    prior_alpha = _positive_finite(prior_alpha, field_name="prior_alpha")
    prior_beta = _positive_finite(prior_beta, field_name="prior_beta")
    if isinstance(ece_bins, bool) or not isinstance(ece_bins, int) or ece_bins <= 0:
        raise ValueError("ece_bins must be a positive integer")
    grouped: dict[str, list[TrialRecord]] = {}
    for raw in records:
        record = raw if isinstance(raw, TrialRecord) else TrialRecord.from_dict(raw)
        grouped.setdefault(record.task_family or "default", []).append(record)

    grid: dict[str, dict[str, CalibrationProfile]] = {}
    for task_family in sorted(grouped):
        profiles = aggregate_calibration(
            grouped[task_family],
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            ece_bins=ece_bins,
        )
        for model_id, profile in profiles.items():
            grid.setdefault(model_id, {})[task_family] = profile
    return grid


__all__ = [
    "CalibrationProfile",
    "TrialRecord",
    "aggregate_calibration",
    "aggregate_calibration_by_task",
    "brier_score",
    "expected_calibration_error",
    "percentile",
    "wilson_interval",
]
