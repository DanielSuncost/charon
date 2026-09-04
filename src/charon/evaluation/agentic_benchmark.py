"""Deterministic evaluation utilities for graph-orchestrated agent systems.

The module intentionally separates trial collection from analysis. Callers can
record trials in any execution environment, then aggregate, compare, and export
the results without depending on a particular model provider or statistics
package.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import random
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence

from charon.routing.calibration import percentile, wilson_interval


type JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list[JsonValue]
    | dict[str, JsonValue]
)

_LOWER_IS_BETTER = frozenset({"cost_usd", "latency_ms"})
_SUMMARY_MAXIMIZE = ("success_rate", "mean_score")
_SUMMARY_MINIMIZE = ("mean_cost_usd", "p95_latency_ms")
_METRICS = frozenset({"success", "score", "cost_usd", "latency_ms"})
_MISSING_PAIR_BEHAVIORS = frozenset({"report", "error"})


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a boolean")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _probability(value: Any, name: str) -> float:
    value = _finite(value, name)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _non_negative(value: Any, name: str) -> float:
    value = _finite(value, name)
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, name)


def _metadata(value: Any, name: str = "metadata") -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable: {exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must be an object")
    return decoded


def _confidence_level(value: Any) -> float:
    confidence = _probability(value, "confidence_level")
    if confidence in {0.0, 1.0}:
        raise ValueError("confidence_level must be strictly between 0 and 1")
    return confidence


@dataclass(frozen=True)
class BenchmarkScenario:
    """A stable unit of benchmark work.

    ``pair_id`` identifies equivalent repetitions across policies. It is
    optional when each policy is evaluated exactly once per scenario.
    """

    scenario_id: str
    task_family: str = "default"
    pair_id: str = "0"
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_string(self.scenario_id, "scenario_id")
        _required_string(self.task_family, "task_family")
        _required_string(self.pair_id, "pair_id")
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BenchmarkScenario:
        if not isinstance(data, Mapping):
            raise ValueError("BenchmarkScenario must be an object")
        if "scenario_id" not in data:
            raise ValueError("scenario_id is required")
        return cls(
            scenario_id=data["scenario_id"],
            task_family=data.get("task_family", "default"),
            pair_id=data.get("pair_id", "0"),
            metadata=_metadata(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "scenario_id": self.scenario_id,
            "task_family": self.task_family,
            "pair_id": self.pair_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BenchmarkTrial:
    """One policy result for one benchmark scenario and paired repetition."""

    scenario_id: str
    policy_id: str
    success: bool
    score: float
    cost_usd: float
    latency_ms: float
    selected_model_id: str | None = None
    oracle_model_id: str | None = None
    task_family: str = "default"
    pair_id: str = "0"
    predicted_success: float | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_string(self.scenario_id, "scenario_id")
        _required_string(self.policy_id, "policy_id")
        _required_string(self.task_family, "task_family")
        _required_string(self.pair_id, "pair_id")
        object.__setattr__(
            self, "success", _strict_bool(self.success, "success")
        )
        _optional_string(self.selected_model_id, "selected_model_id")
        _optional_string(self.oracle_model_id, "oracle_model_id")
        object.__setattr__(self, "score", _probability(self.score, "score"))
        object.__setattr__(
            self, "cost_usd", _non_negative(self.cost_usd, "cost_usd")
        )
        object.__setattr__(
            self, "latency_ms", _non_negative(self.latency_ms, "latency_ms")
        )
        if self.predicted_success is not None:
            object.__setattr__(
                self,
                "predicted_success",
                _probability(self.predicted_success, "predicted_success"),
            )
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    @property
    def pair_key(self) -> tuple[str, str]:
        return self.scenario_id, self.pair_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BenchmarkTrial:
        if not isinstance(data, Mapping):
            raise ValueError("BenchmarkTrial must be an object")
        for name in ("scenario_id", "policy_id"):
            if name not in data:
                raise ValueError(f"{name} is required")
        if "success" not in data:
            raise ValueError("success is required")
        success = _strict_bool(data["success"], "success")
        return cls(
            scenario_id=data["scenario_id"],
            policy_id=data["policy_id"],
            success=success,
            score=data.get("score", float(success)),
            cost_usd=data.get("cost_usd", 0.0),
            latency_ms=data.get("latency_ms", 0.0),
            selected_model_id=data.get("selected_model_id"),
            oracle_model_id=data.get("oracle_model_id"),
            task_family=data.get("task_family", "default"),
            pair_id=data.get("pair_id", data.get("replicate_id", "0")),
            predicted_success=(
                None
                if data.get("predicted_success") is None
                else data["predicted_success"]
            ),
            metadata=_metadata(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "scenario_id": self.scenario_id,
            "policy_id": self.policy_id,
            "success": self.success,
            "score": self.score,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "selected_model_id": self.selected_model_id,
            "oracle_model_id": self.oracle_model_id,
            "task_family": self.task_family,
            "pair_id": self.pair_id,
            "predicted_success": self.predicted_success,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PolicySummary:
    """Scenario-weighted outcome and resource metrics for one policy.

    The headline success interval resamples independent scenarios, not raw
    repetitions. ``trial_success_*`` preserves the conventional trial-level
    estimate and Wilson interval for diagnostic use.
    """

    policy_id: str
    trial_count: int
    scenario_count: int
    success_count: int
    success_rate: float
    success_ci_low: float | None
    success_ci_high: float | None
    trial_success_rate: float
    trial_success_ci_low: float
    trial_success_ci_high: float
    interval_method: str
    interval_status: str
    confidence_level: float
    bootstrap_samples: int
    seed: int
    mean_score: float
    mean_cost_usd: float
    p50_latency_ms: float
    p95_latency_ms: float
    model_selection_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_string(self.policy_id, "policy_id")
        _positive_int(self.trial_count, "trial_count")
        _positive_int(self.scenario_count, "scenario_count")
        _non_negative_int(self.success_count, "success_count")
        if self.scenario_count > self.trial_count:
            raise ValueError("scenario_count cannot exceed trial_count")
        if self.success_count > self.trial_count:
            raise ValueError("success_count cannot exceed trial_count")
        for name in ("success_rate", "trial_success_rate", "mean_score"):
            object.__setattr__(
                self, name, _probability(getattr(self, name), name)
            )
        trial_low = _probability(
            self.trial_success_ci_low, "trial_success_ci_low"
        )
        trial_high = _probability(
            self.trial_success_ci_high, "trial_success_ci_high"
        )
        object.__setattr__(self, "trial_success_ci_low", trial_low)
        object.__setattr__(self, "trial_success_ci_high", trial_high)
        if trial_low > trial_high:
            raise ValueError(
                "trial_success_ci_low cannot exceed trial_success_ci_high"
            )
        if not trial_low <= self.trial_success_rate <= trial_high:
            raise ValueError(
                "trial success interval must contain trial_success_rate"
            )
        if (self.success_ci_low is None) != (self.success_ci_high is None):
            raise ValueError("headline success interval bounds must both be set or null")
        if self.success_ci_low is not None:
            ci_low = _probability(self.success_ci_low, "success_ci_low")
            ci_high = _probability(self.success_ci_high, "success_ci_high")
            object.__setattr__(self, "success_ci_low", ci_low)
            object.__setattr__(self, "success_ci_high", ci_high)
            if ci_low > ci_high:
                raise ValueError("success_ci_low cannot exceed success_ci_high")
        _required_string(self.interval_method, "interval_method")
        if self.interval_status not in {"estimated", "insufficient_scenarios"}:
            raise ValueError(
                "interval_status must be estimated or insufficient_scenarios"
            )
        if self.interval_status == "estimated" and self.success_ci_low is None:
            raise ValueError("estimated intervals require numeric bounds")
        if (
            self.interval_status == "insufficient_scenarios"
            and self.success_ci_low is not None
        ):
            raise ValueError(
                "insufficient_scenarios intervals must use null bounds"
            )
        if self.scenario_count < 2 and self.interval_status != "insufficient_scenarios":
            raise ValueError(
                "fewer than two scenarios require insufficient_scenarios status"
            )
        if self.scenario_count >= 2 and self.interval_status != "estimated":
            raise ValueError(
                "two or more scenarios require an estimated interval"
            )
        object.__setattr__(
            self,
            "confidence_level",
            _confidence_level(self.confidence_level),
        )
        _positive_int(self.bootstrap_samples, "bootstrap_samples")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        for name in ("mean_cost_usd", "p50_latency_ms", "p95_latency_ms"):
            object.__setattr__(
                self, name, _non_negative(getattr(self, name), name)
            )
        if self.p50_latency_ms > self.p95_latency_ms:
            raise ValueError("p50_latency_ms cannot exceed p95_latency_ms")
        if not isinstance(self.model_selection_counts, Mapping):
            raise ValueError("model_selection_counts must be an object")
        counts: dict[str, int] = {}
        for model_id, count in self.model_selection_counts.items():
            _required_string(model_id, "model_selection_counts key")
            counts[model_id] = _non_negative_int(
                count, f"model_selection_counts[{model_id!r}]"
            )
        if sum(counts.values()) > self.trial_count:
            raise ValueError(
                "model selection counts cannot exceed trial_count"
            )
        object.__setattr__(
            self, "model_selection_counts", dict(sorted(counts.items()))
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "policy_id": self.policy_id,
            "trial_count": self.trial_count,
            "scenario_count": self.scenario_count,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "success_ci_low": self.success_ci_low,
            "success_ci_high": self.success_ci_high,
            "trial_success_rate": self.trial_success_rate,
            "trial_success_ci_low": self.trial_success_ci_low,
            "trial_success_ci_high": self.trial_success_ci_high,
            "interval_method": self.interval_method,
            "interval_status": self.interval_status,
            "confidence_level": self.confidence_level,
            "bootstrap_samples": self.bootstrap_samples,
            "seed": self.seed,
            "mean_score": self.mean_score,
            "mean_cost_usd": self.mean_cost_usd,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "model_selection_counts": dict(self.model_selection_counts),
        }


@dataclass(frozen=True)
class PairedDelta:
    """A paired policy delta with a deterministic scenario bootstrap interval."""

    baseline_policy_id: str
    contender_policy_id: str
    metric: str
    baseline_trial_count: int
    contender_trial_count: int
    matched_pair_count: int
    baseline_unmatched_count: int
    contender_unmatched_count: int
    scenario_count: int
    mean_delta: float
    ci_low: float | None
    ci_high: float | None
    interval_status: str
    confidence_level: float
    bootstrap_samples: int
    seed: int
    higher_is_better: bool
    missing_pair_behavior: str

    def __post_init__(self) -> None:
        _required_string(self.baseline_policy_id, "baseline_policy_id")
        _required_string(self.contender_policy_id, "contender_policy_id")
        if self.baseline_policy_id == self.contender_policy_id:
            raise ValueError("baseline and contender policies must differ")
        if self.metric not in _METRICS:
            raise ValueError(
                "metric must be one of: success, score, cost_usd, latency_ms"
            )
        for name in (
            "baseline_trial_count",
            "contender_trial_count",
            "matched_pair_count",
            "baseline_unmatched_count",
            "contender_unmatched_count",
            "scenario_count",
        ):
            _non_negative_int(getattr(self, name), name)
        if self.matched_pair_count < 1:
            raise ValueError("matched_pair_count must be positive")
        if self.scenario_count < 1:
            raise ValueError("scenario_count must be positive")
        if self.scenario_count > self.matched_pair_count:
            raise ValueError("scenario_count cannot exceed matched_pair_count")
        if (
            self.matched_pair_count + self.baseline_unmatched_count
            != self.baseline_trial_count
        ):
            raise ValueError(
                "baseline counts must equal matched plus unmatched trials"
            )
        if (
            self.matched_pair_count + self.contender_unmatched_count
            != self.contender_trial_count
        ):
            raise ValueError(
                "contender counts must equal matched plus unmatched trials"
            )
        object.__setattr__(
            self, "mean_delta", _finite(self.mean_delta, "mean_delta")
        )
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("paired interval bounds must both be set or null")
        if self.ci_low is not None:
            ci_low = _finite(self.ci_low, "ci_low")
            ci_high = _finite(self.ci_high, "ci_high")
            object.__setattr__(self, "ci_low", ci_low)
            object.__setattr__(self, "ci_high", ci_high)
            if ci_low > ci_high:
                raise ValueError("ci_low cannot exceed ci_high")
        if self.interval_status not in {"estimated", "insufficient_scenarios"}:
            raise ValueError(
                "interval_status must be estimated or insufficient_scenarios"
            )
        if self.interval_status == "estimated" and self.ci_low is None:
            raise ValueError("estimated intervals require numeric bounds")
        if (
            self.interval_status == "insufficient_scenarios"
            and self.ci_low is not None
        ):
            raise ValueError(
                "insufficient_scenarios intervals must use null bounds"
            )
        if self.scenario_count < 2 and self.interval_status != "insufficient_scenarios":
            raise ValueError(
                "fewer than two scenarios require insufficient_scenarios status"
            )
        if self.scenario_count >= 2 and self.interval_status != "estimated":
            raise ValueError(
                "two or more scenarios require an estimated interval"
            )
        object.__setattr__(
            self,
            "confidence_level",
            _confidence_level(self.confidence_level),
        )
        _positive_int(self.bootstrap_samples, "bootstrap_samples")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        object.__setattr__(
            self,
            "higher_is_better",
            _strict_bool(self.higher_is_better, "higher_is_better"),
        )
        if self.missing_pair_behavior not in _MISSING_PAIR_BEHAVIORS:
            raise ValueError(
                "missing_pair_behavior must be report or error"
            )

    @property
    def pair_count(self) -> int:
        """Backward-compatible alias for ``matched_pair_count``."""

        return self.matched_pair_count

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "baseline_policy_id": self.baseline_policy_id,
            "contender_policy_id": self.contender_policy_id,
            "metric": self.metric,
            "baseline_trial_count": self.baseline_trial_count,
            "contender_trial_count": self.contender_trial_count,
            "matched_pair_count": self.matched_pair_count,
            "pair_count": self.matched_pair_count,
            "baseline_unmatched_count": self.baseline_unmatched_count,
            "contender_unmatched_count": self.contender_unmatched_count,
            "scenario_count": self.scenario_count,
            "mean_delta": self.mean_delta,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "interval_status": self.interval_status,
            "confidence_level": self.confidence_level,
            "bootstrap_samples": self.bootstrap_samples,
            "seed": self.seed,
            "higher_is_better": self.higher_is_better,
            "missing_pair_behavior": self.missing_pair_behavior,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable benchmark aggregates, comparisons, and routing diagnostics."""

    summaries: dict[str, PolicySummary]
    paired_deltas: tuple[PairedDelta, ...]
    pareto_policy_ids: tuple[str, ...]
    routing_matrix: dict[str, JsonValue]
    pairing: dict[str, JsonValue] = field(default_factory=dict)
    config: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.summaries, Mapping):
            raise ValueError("summaries must be an object")
        summaries = dict(self.summaries)
        if not summaries:
            raise ValueError("summaries must not be empty")
        for policy_id, summary in summaries.items():
            _required_string(policy_id, "summary policy id")
            if not isinstance(summary, PolicySummary):
                raise ValueError("summaries must contain PolicySummary values")
            if summary.policy_id != policy_id:
                raise ValueError("summary keys must match PolicySummary.policy_id")
        deltas = tuple(self.paired_deltas)
        if any(not isinstance(delta, PairedDelta) for delta in deltas):
            raise ValueError("paired_deltas must contain PairedDelta values")
        for delta in deltas:
            if (
                delta.baseline_policy_id not in summaries
                or delta.contender_policy_id not in summaries
            ):
                raise ValueError("paired delta references an unknown policy")
        pareto = tuple(self.pareto_policy_ids)
        if len(pareto) != len(set(pareto)):
            raise ValueError("pareto_policy_ids must be unique")
        if not set(pareto) <= set(summaries):
            raise ValueError("pareto_policy_ids contains an unknown policy")
        object.__setattr__(self, "summaries", summaries)
        object.__setattr__(self, "paired_deltas", deltas)
        object.__setattr__(self, "pareto_policy_ids", pareto)
        object.__setattr__(
            self,
            "routing_matrix",
            _metadata(self.routing_matrix, "routing_matrix"),
        )
        object.__setattr__(
            self, "pairing", _metadata(self.pairing, "pairing")
        )
        object.__setattr__(
            self, "config", _metadata(self.config, "config")
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "summaries": {
                policy_id: self.summaries[policy_id].to_dict()
                for policy_id in sorted(self.summaries)
            },
            "paired_deltas": [delta.to_dict() for delta in self.paired_deltas],
            "pareto_policy_ids": list(self.pareto_policy_ids),
            "routing_matrix": dict(self.routing_matrix),
            "pairing": dict(self.pairing),
            "config": dict(self.config),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def write_json(self, path: str | Path, *, indent: int | None = 2) -> Path:
        output_path = Path(path)
        output_path.write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")
        return output_path


def _scenario_cluster_interval(
    scenario_values: Mapping[str, Sequence[float]],
    *,
    bootstrap_samples: int,
    seed: int,
    confidence_level: float,
) -> tuple[float, float | None, float | None, str]:
    """Return a scenario-macro mean and clustered percentile interval."""

    scenario_means = [
        fmean(scenario_values[scenario_id])
        for scenario_id in sorted(scenario_values)
    ]
    point = fmean(scenario_means)
    if len(scenario_means) < 2:
        return point, None, None, "insufficient_scenarios"

    rng = random.Random(seed)
    cluster_count = len(scenario_means)
    bootstrapped = [
        fmean(
            scenario_means[rng.randrange(cluster_count)]
            for _ in range(cluster_count)
        )
        for _ in range(bootstrap_samples)
    ]
    alpha = (1.0 - confidence_level) / 2.0
    return (
        point,
        percentile(bootstrapped, alpha),
        percentile(bootstrapped, 1.0 - alpha),
        "estimated",
    )


def summarize_policy_trials(
    trials: Iterable[BenchmarkTrial | Mapping[str, Any]],
    *,
    bootstrap_samples: int = 2_000,
    seed: int = 0,
    confidence_level: float = 0.95,
) -> dict[str, PolicySummary]:
    """Aggregate policies without treating repetitions as independent tasks.

    Success, score, and cost are macro-averaged across scenarios. The headline
    success interval uses a deterministic scenario-cluster bootstrap. With
    fewer than two independent scenarios, its bounds are ``None`` and
    ``interval_status`` is ``"insufficient_scenarios"``. Trial-level success
    and Wilson bounds remain available as explicitly labeled diagnostics.
    """

    _positive_int(bootstrap_samples, "bootstrap_samples")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    confidence_level = _confidence_level(confidence_level)

    grouped: dict[str, list[BenchmarkTrial]] = defaultdict(list)
    for value in trials:
        trial = (
            value
            if isinstance(value, BenchmarkTrial)
            else BenchmarkTrial.from_dict(value)
        )
        grouped[trial.policy_id].append(trial)

    summaries: dict[str, PolicySummary] = {}
    for policy_index, policy_id in enumerate(sorted(grouped)):
        policy_trials = grouped[policy_id]
        successes = sum(trial.success for trial in policy_trials)
        trial_count = len(policy_trials)
        trial_ci_low, trial_ci_high = wilson_interval(successes, trial_count)
        by_scenario: dict[str, list[BenchmarkTrial]] = defaultdict(list)
        for trial in policy_trials:
            by_scenario[trial.scenario_id].append(trial)
        for scenario_id, scenario_trials in by_scenario.items():
            families = {trial.task_family for trial in scenario_trials}
            if len(families) != 1:
                raise ValueError(
                    f"policy {policy_id!r} scenario {scenario_id!r} "
                    "contains multiple task_family values"
                )
        policy_seed = seed + policy_index
        success_rate, ci_low, ci_high, interval_status = (
            _scenario_cluster_interval(
                {
                    scenario_id: [
                        float(trial.success) for trial in scenario_trials
                    ]
                    for scenario_id, scenario_trials in by_scenario.items()
                },
                bootstrap_samples=bootstrap_samples,
                seed=policy_seed,
                confidence_level=confidence_level,
            )
        )
        models = Counter(
            trial.selected_model_id
            for trial in policy_trials
            if trial.selected_model_id is not None
        )
        summaries[policy_id] = PolicySummary(
            policy_id=policy_id,
            trial_count=trial_count,
            scenario_count=len(by_scenario),
            success_count=successes,
            success_rate=success_rate,
            success_ci_low=ci_low,
            success_ci_high=ci_high,
            trial_success_rate=successes / trial_count,
            trial_success_ci_low=trial_ci_low,
            trial_success_ci_high=trial_ci_high,
            interval_method="scenario_cluster_bootstrap_percentile",
            interval_status=interval_status,
            confidence_level=confidence_level,
            bootstrap_samples=bootstrap_samples,
            seed=policy_seed,
            mean_score=fmean(
                fmean(trial.score for trial in by_scenario[scenario_id])
                for scenario_id in sorted(by_scenario)
            ),
            mean_cost_usd=fmean(
                fmean(trial.cost_usd for trial in by_scenario[scenario_id])
                for scenario_id in sorted(by_scenario)
            ),
            p50_latency_ms=percentile(
                [
                    fmean(
                        trial.latency_ms
                        for trial in by_scenario[scenario_id]
                    )
                    for scenario_id in sorted(by_scenario)
                ],
                0.50,
            ),
            p95_latency_ms=percentile(
                [
                    fmean(
                        trial.latency_ms
                        for trial in by_scenario[scenario_id]
                    )
                    for scenario_id in sorted(by_scenario)
                ],
                0.95,
            ),
            model_selection_counts=dict(sorted(models.items())),
        )
    return summaries


def _metric_value(trial: BenchmarkTrial, metric: str) -> float:
    if metric == "success":
        return float(trial.success)
    if metric not in _METRICS:
        raise ValueError(
            "metric must be one of: success, score, cost_usd, latency_ms"
        )
    return float(getattr(trial, metric))


def paired_bootstrap_delta(
    trials: Iterable[BenchmarkTrial | Mapping[str, Any]],
    *,
    baseline_policy_id: str,
    contender_policy_id: str,
    metric: str = "success",
    bootstrap_samples: int = 2_000,
    seed: int = 0,
    confidence_level: float = 0.95,
    missing_pair_behavior: str = "report",
) -> PairedDelta:
    """Compare two policies on matched scenario/repetition pairs.

    Resampling is clustered by scenario so repeated runs of one scenario do not
    masquerade as independent tasks. The reported delta is always
    ``contender - baseline``; ``higher_is_better`` makes its direction explicit.

    ``missing_pair_behavior="report"`` analyzes the intersection and reports
    unmatched counts. ``"error"`` requires identical pair-key coverage. A pair
    whose policies disagree on ``task_family`` is always rejected.
    """

    _required_string(baseline_policy_id, "baseline_policy_id")
    _required_string(contender_policy_id, "contender_policy_id")
    if baseline_policy_id == contender_policy_id:
        raise ValueError("baseline and contender policies must differ")
    if metric not in _METRICS:
        raise ValueError(
            "metric must be one of: success, score, cost_usd, latency_ms"
        )
    _positive_int(bootstrap_samples, "bootstrap_samples")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    confidence_level = _confidence_level(confidence_level)
    if missing_pair_behavior not in _MISSING_PAIR_BEHAVIORS:
        raise ValueError("missing_pair_behavior must be report or error")

    indexed: dict[str, dict[tuple[str, str], BenchmarkTrial]] = {
        baseline_policy_id: {},
        contender_policy_id: {},
    }
    for value in trials:
        trial = (
            value
            if isinstance(value, BenchmarkTrial)
            else BenchmarkTrial.from_dict(value)
        )
        if trial.policy_id not in indexed:
            continue
        if trial.pair_key in indexed[trial.policy_id]:
            raise ValueError(
                "duplicate trial for policy and pair: "
                f"{trial.policy_id} {trial.pair_key!r}"
            )
        indexed[trial.policy_id][trial.pair_key] = trial

    baseline_keys = set(indexed[baseline_policy_id])
    contender_keys = set(indexed[contender_policy_id])
    common_keys = sorted(baseline_keys & contender_keys)
    baseline_unmatched = sorted(baseline_keys - contender_keys)
    contender_unmatched = sorted(contender_keys - baseline_keys)
    if (
        missing_pair_behavior == "error"
        and (baseline_unmatched or contender_unmatched)
    ):
        raise ValueError(
            "policy pair coverage differs: "
            f"{len(baseline_unmatched)} baseline-only and "
            f"{len(contender_unmatched)} contender-only pairs"
        )
    if not common_keys:
        raise ValueError("the policies have no matched trial pairs")

    by_scenario: dict[str, list[float]] = defaultdict(list)
    for key in common_keys:
        baseline = indexed[baseline_policy_id][key]
        contender = indexed[contender_policy_id][key]
        if baseline.task_family != contender.task_family:
            raise ValueError(
                "matched trials disagree on task_family for pair "
                f"{key!r}: {baseline.task_family!r} != "
                f"{contender.task_family!r}"
            )
        baseline_seed = baseline.metadata.get("seed")
        contender_seed = contender.metadata.get("seed")
        if baseline_seed != contender_seed:
            raise ValueError(
                "matched trials disagree on metadata.seed for pair "
                f"{key!r}: {baseline_seed!r} != {contender_seed!r}"
            )
        by_scenario[key[0]].append(
            _metric_value(contender, metric) - _metric_value(baseline, metric)
        )

    point_estimate, ci_low, ci_high, interval_status = (
        _scenario_cluster_interval(
            by_scenario,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            confidence_level=confidence_level,
        )
    )

    return PairedDelta(
        baseline_policy_id=baseline_policy_id,
        contender_policy_id=contender_policy_id,
        metric=metric,
        baseline_trial_count=len(baseline_keys),
        contender_trial_count=len(contender_keys),
        matched_pair_count=len(common_keys),
        baseline_unmatched_count=len(baseline_unmatched),
        contender_unmatched_count=len(contender_unmatched),
        scenario_count=len(by_scenario),
        mean_delta=point_estimate,
        ci_low=ci_low,
        ci_high=ci_high,
        interval_status=interval_status,
        confidence_level=confidence_level,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        higher_is_better=metric not in _LOWER_IS_BETTER,
        missing_pair_behavior=missing_pair_behavior,
    )


def pareto_frontier(
    summaries: Mapping[str, PolicySummary | Mapping[str, Any]],
    *,
    maximize: Sequence[str] = _SUMMARY_MAXIMIZE,
    minimize: Sequence[str] = _SUMMARY_MINIMIZE,
) -> tuple[str, ...]:
    """Return the deterministic set of non-dominated policy identifiers."""

    if not maximize and not minimize:
        raise ValueError("at least one Pareto metric is required")

    values: dict[str, dict[str, float]] = {}
    required = tuple(maximize) + tuple(minimize)
    for policy_id in sorted(summaries):
        summary = summaries[policy_id]
        raw = summary.to_dict() if isinstance(summary, PolicySummary) else summary
        values[policy_id] = {
            metric: _finite(raw[metric], metric) for metric in required
        }

    def dominates(left: str, right: str) -> bool:
        no_worse = all(
            values[left][metric] >= values[right][metric] for metric in maximize
        ) and all(
            values[left][metric] <= values[right][metric] for metric in minimize
        )
        strictly_better = any(
            values[left][metric] > values[right][metric] for metric in maximize
        ) or any(
            values[left][metric] < values[right][metric] for metric in minimize
        )
        return no_worse and strictly_better

    return tuple(
        candidate
        for candidate in sorted(values)
        if not any(
            dominates(other, candidate)
            for other in sorted(values)
            if other != candidate
        )
    )


def routing_matrix_data(
    trials: Iterable[BenchmarkTrial | Mapping[str, Any]],
) -> dict[str, JsonValue]:
    """Build policy/model counts and selected-versus-oracle confusion data."""

    normalized = [
        (
            value
            if isinstance(value, BenchmarkTrial)
            else BenchmarkTrial.from_dict(value)
        )
        for value in trials
    ]
    policy_ids = sorted({trial.policy_id for trial in normalized})
    model_ids = sorted(
        {
            trial.selected_model_id
            for trial in normalized
            if trial.selected_model_id is not None
        }
    )
    counts: dict[str, dict[str, int]] = {
        policy_id: {model_id: 0 for model_id in model_ids}
        for policy_id in policy_ids
    }
    for trial in normalized:
        if trial.selected_model_id is not None:
            counts[trial.policy_id][trial.selected_model_id] += 1

    oracle_trials = [
        trial
        for trial in normalized
        if trial.oracle_model_id is not None and trial.selected_model_id is not None
    ]
    confusion_labels = sorted(
        {
            model_id
            for trial in oracle_trials
            for model_id in (trial.oracle_model_id, trial.selected_model_id)
            if model_id is not None
        }
    )
    confusion = {
        oracle_id: {selected_id: 0 for selected_id in confusion_labels}
        for oracle_id in confusion_labels
    }
    matched = 0
    for trial in oracle_trials:
        assert trial.oracle_model_id is not None
        assert trial.selected_model_id is not None
        confusion[trial.oracle_model_id][trial.selected_model_id] += 1
        matched += trial.oracle_model_id == trial.selected_model_id

    return {
        "policy_ids": policy_ids,
        "model_ids": model_ids,
        "policy_model_counts": counts,
        "policy_model_matrix": [
            [counts[policy_id][model_id] for model_id in model_ids]
            for policy_id in policy_ids
        ],
        "confusion_labels": confusion_labels,
        "oracle_selected_counts": confusion,
        "oracle_selected_matrix": [
            [confusion[oracle_id][selected_id] for selected_id in confusion_labels]
            for oracle_id in confusion_labels
        ],
        "oracle_match_count": matched,
        "oracle_trial_count": len(oracle_trials),
        "oracle_accuracy": (
            matched / len(oracle_trials) if oracle_trials else None
        ),
    }


class AgenticBenchmark:
    """Aggregate paired trials into a reproducible analysis report."""

    def __init__(
        self,
        trials: Iterable[BenchmarkTrial | Mapping[str, Any]] = (),
    ) -> None:
        self._trials = tuple(
            value
            if isinstance(value, BenchmarkTrial)
            else BenchmarkTrial.from_dict(value)
            for value in trials
        )

    @property
    def trials(self) -> tuple[BenchmarkTrial, ...]:
        return self._trials

    def report(
        self,
        *,
        baseline_policy_id: str | None = None,
        metrics: Sequence[str] = ("success", "score", "cost_usd", "latency_ms"),
        bootstrap_samples: int = 2_000,
        seed: int = 0,
        confidence_level: float = 0.95,
        missing_pair_behavior: str = "report",
    ) -> BenchmarkReport:
        metric_names = tuple(metrics)
        if not metric_names:
            raise ValueError("at least one comparison metric is required")
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("comparison metrics must be unique")
        unknown_metrics = set(metric_names) - _METRICS
        if unknown_metrics:
            raise ValueError(
                "unknown comparison metric(s): "
                + ", ".join(sorted(unknown_metrics))
            )
        _positive_int(bootstrap_samples, "bootstrap_samples")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        confidence_level = _confidence_level(confidence_level)
        if missing_pair_behavior not in _MISSING_PAIR_BEHAVIORS:
            raise ValueError("missing_pair_behavior must be report or error")

        summaries = summarize_policy_trials(
            self._trials,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            confidence_level=confidence_level,
        )
        policy_ids = sorted(summaries)
        if not policy_ids:
            raise ValueError("at least one benchmark trial is required")
        baseline = (
            baseline_policy_id
            if baseline_policy_id is not None
            else policy_ids[0]
        )
        _required_string(baseline, "baseline_policy_id")
        if baseline not in summaries:
            raise ValueError(f"unknown baseline policy: {baseline}")

        deltas: list[PairedDelta] = []
        pairing_comparisons: dict[str, JsonValue] = {}
        comparison_index = 0
        for contender in policy_ids:
            if contender == baseline:
                continue
            for metric in metric_names:
                delta = paired_bootstrap_delta(
                    self._trials,
                    baseline_policy_id=baseline,
                    contender_policy_id=contender,
                    metric=metric,
                    bootstrap_samples=bootstrap_samples,
                    seed=seed + comparison_index,
                    confidence_level=confidence_level,
                    missing_pair_behavior=missing_pair_behavior,
                )
                deltas.append(delta)
                if contender not in pairing_comparisons:
                    pairing_comparisons[contender] = {
                        "baseline_trial_count": delta.baseline_trial_count,
                        "contender_trial_count": delta.contender_trial_count,
                        "matched_pair_count": delta.matched_pair_count,
                        "baseline_unmatched_count": (
                            delta.baseline_unmatched_count
                        ),
                        "contender_unmatched_count": (
                            delta.contender_unmatched_count
                        ),
                        "matched_scenario_count": delta.scenario_count,
                    }
                comparison_index += 1

        return BenchmarkReport(
            summaries=summaries,
            paired_deltas=tuple(deltas),
            pareto_policy_ids=pareto_frontier(summaries),
            routing_matrix=routing_matrix_data(self._trials),
            pairing={
                "baseline_policy_id": baseline,
                "missing_pair_behavior": missing_pair_behavior,
                "comparisons": pairing_comparisons,
            },
            config={
                "baseline_policy_id": baseline,
                "metrics": list(metric_names),
                "bootstrap_samples": bootstrap_samples,
                "seed": seed,
                "confidence_level": confidence_level,
                "summary_interval_method": (
                    "scenario_cluster_bootstrap_percentile"
                ),
                "missing_pair_behavior": missing_pair_behavior,
            },
        )


__all__ = [
    "AgenticBenchmark",
    "BenchmarkReport",
    "BenchmarkScenario",
    "BenchmarkTrial",
    "PairedDelta",
    "PolicySummary",
    "paired_bootstrap_delta",
    "pareto_frontier",
    "routing_matrix_data",
    "summarize_policy_trials",
]
