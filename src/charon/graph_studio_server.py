"""Local control plane and static server for Charon Graph Studio."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import secrets
import threading
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

from charon.orchestration.graph_executors import register_builtin_executors
from charon.orchestration.graph_runtime import (
    get_run,
    list_runs,
    start_run,
    stop_run,
    tick_run,
)
from charon.orchestration.graph_schema import GraphDefinition


CONTROL_HEADER = "X-Charon-Control-Token"
_DEMO_TAG = "charon.graph_studio.demo"
_MAX_BODY_BYTES = 64 * 1024
_MAX_RUN_TICKS = 512
_MAX_EVENTS = 500
_TYPE_MAP = {
    "coordinator": "agent",
    "merge": "tool",
    "judge": "evaluator",
    "checkpoint": "output",
}
_ALLOWED_UI_TYPES = {
    "router",
    "agent",
    "tool",
    "evaluator",
    "gate",
    "input",
    "output",
}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "stopped"}
_ACTIVE_RUN_STATUSES = {"running", "suspended"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"required file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _contained_file(root: Path, relative: str) -> Path:
    """Resolve a trusted repo-relative asset without permitting escape."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("asset path must be a non-empty string")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"asset path escapes repository root: {relative!r}") from exc
    if not candidate.is_file():
        raise ValueError(f"asset is not a file: {relative!r}")
    return candidate


def load_demo_assets(
    repo_root: Path | None = None,
    *,
    workflow_path: Path | None = None,
    benchmark_path: Path | None = None,
) -> tuple[GraphDefinition, dict[str, Any], dict[str, Any]]:
    """Load, expand, and validate the checked-in demonstration assets."""
    root = Path(repo_root or _repo_root()).resolve()
    workflow_file = Path(
        workflow_path or root / "examples" / "graph_workflows" / "software-delivery.json"
    ).resolve()
    benchmark_file = Path(
        benchmark_path or root / "examples" / "routing" / "calibration-fixture.json"
    ).resolve()
    workflow = _read_json(workflow_file)
    benchmark = _read_json(benchmark_file)
    expanded = copy.deepcopy(workflow)

    for node in expanded.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.get("config")
        if not isinstance(config, dict) or not config.get("candidate_source"):
            continue
        source_path = _contained_file(root, str(config["candidate_source"]))
        source = _read_json(source_path)
        candidates = source.get("models")
        if not isinstance(candidates, list):
            raise ValueError(
                f"candidate source {source_path} does not contain a models array"
            )
        config["candidates"] = copy.deepcopy(candidates)

    definition = GraphDefinition.from_dict(expanded)
    return definition, workflow, benchmark


def _finite_metric(
    value: Any,
    field_name: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric, not boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{field_name} must be finite and at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return number


def _validate_benchmark_asset(
    root: Path,
    benchmark: dict[str, Any],
) -> dict[str, Any]:
    from charon.routing import ModelCandidate, PolicyName

    models_raw = benchmark.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ValueError("benchmark models must be a non-empty array")
    models = [ModelCandidate.from_dict(item) for item in models_raw]
    model_ids = [model.candidate_id for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise ValueError("benchmark candidate IDs must be unique")

    policies = benchmark.get("policies")
    if not isinstance(policies, list) or not policies:
        raise ValueError("benchmark policies must be a non-empty array")
    policy_ids: list[str] = []
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict):
            raise ValueError(f"benchmark policy {index} must be an object")
        policy_id = str(policy.get("policy_id") or policy.get("id") or "")
        try:
            PolicyName(policy_id)
        except ValueError as exc:
            raise ValueError(f"unknown bundled policy {policy_id!r}") from exc
        policy_ids.append(policy_id)
        trials = policy.get("trials", policy.get("trial_count"))
        if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
            raise ValueError(f"policy {policy_id!r} trials must be positive")
        scenarios = policy.get("scenarios", policy.get("scenario_count"))
        if scenarios is not None and (
            isinstance(scenarios, bool)
            or not isinstance(scenarios, int)
            or scenarios < 1
            or scenarios > trials
        ):
            raise ValueError(
                f"policy {policy_id!r} scenarios must be in [1, trials]"
            )
        rate = _finite_metric(
            policy.get("success_rate"),
            f"policy {policy_id!r} success_rate",
            maximum=1.0,
        )
        interval = policy.get("success_ci")
        if (
            not isinstance(interval, list)
            or len(interval) != 2
        ):
            raise ValueError(
                f"policy {policy_id!r} success_ci must contain two bounds"
            )
        low = _finite_metric(
            interval[0],
            f"policy {policy_id!r} success_ci[0]",
            maximum=1.0,
        )
        high = _finite_metric(
            interval[1],
            f"policy {policy_id!r} success_ci[1]",
            maximum=1.0,
        )
        if low > high or not low <= rate <= high:
            raise ValueError(
                f"policy {policy_id!r} success interval is inconsistent"
            )
        p50 = _finite_metric(
            policy.get("p50_latency_ms"),
            f"policy {policy_id!r} p50_latency_ms",
        )
        p95 = _finite_metric(
            policy.get("p95_latency_ms"),
            f"policy {policy_id!r} p95_latency_ms",
        )
        if p50 > p95:
            raise ValueError(
                f"policy {policy_id!r} p50 latency exceeds p95 latency"
            )
        for field_name in ("mean_score", "expected_calibration_error"):
            _finite_metric(
                policy.get(field_name),
                f"policy {policy_id!r} {field_name}",
                maximum=1.0,
            )
        _finite_metric(
            policy.get("mean_cost_usd"),
            f"policy {policy_id!r} mean_cost_usd",
        )
        if "recommended" in policy and not isinstance(
            policy["recommended"], bool
        ):
            raise ValueError(
                f"policy {policy_id!r} recommended must be boolean"
            )
    if len(policy_ids) != len(set(policy_ids)):
        raise ValueError("benchmark policy IDs must be unique")
    policy_by_id = {
        str(policy.get("policy_id") or policy.get("id")): policy
        for policy in policies
    }

    recommendation = benchmark.get("recommendation")
    if recommendation is not None:
        if not isinstance(recommendation, dict):
            raise ValueError("benchmark recommendation must be an object")
        eligibility = recommendation.get("eligibility")
        if not isinstance(eligibility, dict):
            raise ValueError("recommendation eligibility must be an object")
        cost_limit = _finite_metric(
            eligibility.get("mean_cost_usd_lte"),
            "recommendation mean_cost_usd_lte",
        )
        latency_limit = _finite_metric(
            eligibility.get("p95_latency_ms_lte"),
            "recommendation p95_latency_ms_lte",
        )
        eligible = sorted(
            policy_id
            for policy_id, policy in policy_by_id.items()
            if float(policy["mean_cost_usd"]) <= cost_limit
            and float(policy["p95_latency_ms"]) <= latency_limit
        )
        if eligible != recommendation.get("eligible_policy_ids"):
            raise ValueError("recommendation eligible policy IDs are inconsistent")
        if not eligible:
            raise ValueError("recommendation operating envelope is empty")
        selected_policy_id = sorted(
            eligible,
            key=lambda policy_id: (
                -float(policy_by_id[policy_id]["success_rate"]),
                -float(policy_by_id[policy_id]["mean_score"]),
                policy_id,
            ),
        )[0]
        if selected_policy_id != recommendation.get("selected_policy_id"):
            raise ValueError("recommendation selected policy is inconsistent")
        marked = sorted(
            policy_id
            for policy_id, policy in policy_by_id.items()
            if policy.get("recommended") is True
        )
        if marked != [selected_policy_id]:
            raise ValueError(
                "exactly the selected recommendation must be marked recommended"
            )

    matrix = benchmark.get("routing_matrix")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("benchmark routing_matrix must be a non-empty array")
    shares: dict[tuple[str, str], dict[str, float]] = {}
    for index, row in enumerate(matrix):
        if not isinstance(row, dict):
            raise ValueError(f"routing matrix row {index} must be an object")
        policy_id = str(row.get("policy_id") or "")
        task_class = str(row.get("task_class") or "")
        model_id = str(row.get("model_id") or "")
        if policy_id not in policy_ids:
            raise ValueError(
                f"routing matrix references unknown policy {policy_id!r}"
            )
        if model_id not in model_ids:
            raise ValueError(
                f"routing matrix references unknown model {model_id!r}"
            )
        if not task_class:
            raise ValueError("routing matrix task_class is required")
        value = _finite_metric(
            row.get("share"),
            f"routing matrix row {index} share",
            maximum=1.0,
        )
        group = shares.setdefault((policy_id, task_class), {})
        if model_id in group:
            raise ValueError(
                "routing matrix contains a duplicate policy/task/model cell"
            )
        group[model_id] = value
    for (policy_id, task_class), group in shares.items():
        if set(group) != set(model_ids):
            raise ValueError(
                f"routing matrix {policy_id!r}/{task_class!r} "
                "must contain every model"
            )
        if not math.isclose(sum(group.values()), 1.0, abs_tol=1e-6):
            raise ValueError(
                f"routing matrix {policy_id!r}/{task_class!r} shares "
                "must sum to 1"
            )

    verified_records = 0
    if benchmark.get("data_kind") == "generated_fixture":
        provenance = benchmark.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("generated fixtures require provenance")
        generator = _contained_file(root, str(provenance.get("generator_path") or ""))
        if not generator.name.endswith(".py"):
            raise ValueError("fixture generator_path must reference a Python file")
        config_digest = provenance.get("config_sha256")
        if (
            not isinstance(config_digest, str)
            or len(config_digest) != 64
            or any(character not in "0123456789abcdef" for character in config_digest)
        ):
            raise ValueError("fixture config_sha256 must be a lowercase SHA-256")
        code_hashes = provenance.get("code_sha256")
        if not isinstance(code_hashes, dict) or not code_hashes:
            raise ValueError("generated fixtures require code hash provenance")
        for relative, expected in code_hashes.items():
            path = _contained_file(root, str(relative))
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"fixture code hash mismatch for {relative!r}")
        corpora = provenance.get("raw_corpora")
        if not isinstance(corpora, dict) or not corpora:
            raise ValueError("generated fixtures require raw corpus provenance")
        for corpus_name, spec in corpora.items():
            if not isinstance(spec, dict):
                raise ValueError(
                    f"raw corpus {corpus_name!r} provenance must be an object"
                )
            path = _contained_file(root, str(spec.get("path") or ""))
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != spec.get("sha256"):
                raise ValueError(
                    f"fixture raw corpus hash mismatch for {corpus_name!r}"
                )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            if not all(isinstance(record, dict) for record in records):
                raise ValueError(
                    f"fixture raw corpus {corpus_name!r} must contain objects"
                )
            if len(records) != spec.get("records"):
                raise ValueError(
                    f"fixture raw corpus count mismatch for {corpus_name!r}"
                )
            verified_records += len(records)

    return {
        "models": len(models),
        "policies": len(policies),
        "routing_profiles": len(shares),
        "verified_raw_records": verified_records,
    }


def validate_demo_assets(
    repo_root: Path | None = None,
    *,
    workflow_path: Path | None = None,
    benchmark_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve()
    definition, _, benchmark = load_demo_assets(
        repo_root,
        workflow_path=workflow_path,
        benchmark_path=benchmark_path,
    )
    benchmark_summary = _validate_benchmark_asset(root, benchmark)
    from charon.routing import route_models

    route_selections = {}
    for node in definition.nodes:
        if node.handler != "route" or "candidates" not in node.config:
            continue
        decision = route_models(
            node.config.get("task") or {},
            node.config["candidates"],
            policy=node.config.get("policy") or "balanced",
        )
        if not decision.feasible:
            raise ValueError(f"demo route node {node.id!r} has no feasible model")
        route_selections[node.id] = decision.selected_candidate_id

    template_path = (
        root / "examples" / "graph_workflows" / "routed-worker.json"
    )
    template = GraphDefinition.from_dict(_read_json(template_path))
    template_nodes = {node.id: node for node in template.nodes}
    route_node = template_nodes.get("select_model")
    queue_node = template_nodes.get("execute_task")
    if (
        route_node is None
        or route_node.handler != "route"
        or queue_node is None
        or queue_node.handler != "queue_agent"
        or queue_node.config.get("route_from")
        != route_node.config.get("state_key")
    ):
        raise ValueError(
            "routed-worker template must connect route output to queue_agent"
        )
    from charon.providers.provider_bridge import ROUTE_PROVIDER_ALIASES

    template_providers = sorted(
        {
            str(candidate.get("provider") or "")
            for candidate in route_node.config.get("candidates") or []
            if isinstance(candidate, dict)
        }
    )
    unsupported = [
        provider
        for provider in template_providers
        if provider not in ROUTE_PROVIDER_ALIASES
    ]
    if unsupported:
        raise ValueError(
            "routed-worker template uses unsupported providers: "
            + ", ".join(unsupported)
        )
    return {
        "ok": True,
        "graph_id": definition.graph_id,
        "nodes": len(definition.nodes),
        "edges": len(definition.edges),
        **benchmark_summary,
        "route_selections": route_selections,
        "executable_template": {
            "graph_id": template.graph_id,
            "nodes": len(template.nodes),
            "edges": len(template.edges),
            "providers": template_providers,
        },
    }


def _humanize(value: str) -> str:
    return " ".join(part for part in str(value).replace("_", "-").split("-") if part).title()


def _pareto_policy_ids(policies: list[dict[str, Any]]) -> set[str]:
    """Match evaluation.pareto_frontier's public four-field definition.

    Importing the evaluation package is intentionally avoided here so the
    stdlib-only server can run in minimal environments.
    """
    maximize = ("success_rate", "mean_score")
    minimize = ("mean_cost_usd", "p95_latency_ms")
    values: dict[str, dict[str, float]] = {}
    for policy in policies:
        policy_id = str(policy.get("policy_id") or policy.get("id") or "")
        if not policy_id:
            continue
        values[policy_id] = {
            field: float(policy.get(field) or 0.0)
            for field in maximize + minimize
        }

    def dominates(left: str, right: str) -> bool:
        no_worse = all(
            values[left][field] >= values[right][field] for field in maximize
        ) and all(
            values[left][field] <= values[right][field] for field in minimize
        )
        strictly_better = any(
            values[left][field] > values[right][field] for field in maximize
        ) or any(
            values[left][field] < values[right][field] for field in minimize
        )
        return no_worse and strictly_better

    return {
        candidate
        for candidate in sorted(values)
        if not any(
            dominates(other, candidate)
            for other in sorted(values)
            if other != candidate
        )
    }


def normalize_benchmark(raw: dict[str, Any]) -> dict[str, Any]:
    policies_raw = [
        copy.deepcopy(item)
        for item in raw.get("policies") or []
        if isinstance(item, dict)
    ]
    pareto_ids = _pareto_policy_ids(policies_raw)
    generated = "fixture" in str(raw.get("data_kind") or "").lower()
    default_description = (
        "Held-out generated policy profile."
        if generated
        else "Empirical routing policy profile."
    )
    policies = []
    for item in policies_raw:
        policy_id = str(item.get("policy_id") or item.get("id") or "")
        normalized = {
            "id": policy_id,
            "name": str(item.get("name") or _humanize(policy_id)),
            "description": str(
                item.get("description") or default_description
            ),
            "samples": int(item.get("trials") or item.get("samples") or 0),
            "trials": int(item.get("trials") or item.get("samples") or 0),
            "scenarios": int(item.get("scenarios") or 0),
            "repetitions_per_scenario": int(
                item.get("repetitions_per_scenario") or 0
            ),
            "pass_rate": float(
                item.get("success_rate", item.get("pass_rate", 0.0)) or 0.0
            ),
            "quality_mean": float(
                item.get("mean_score", item.get("quality_mean", 0.0)) or 0.0
            ),
            "quality_ci": list(
                item.get("success_ci") or item.get("quality_ci") or [0.0, 0.0]
            ),
            "cost_mean": float(
                item.get("mean_cost_usd", item.get("cost_mean", 0.0)) or 0.0
            ),
            "latency_p50_ms": float(
                item.get("p50_latency_ms", item.get("latency_p50_ms", 0.0))
                or 0.0
            ),
            "latency_p95_ms": float(
                item.get("p95_latency_ms", item.get("latency_p95_ms", 0.0))
                or 0.0
            ),
            "calibration_error": float(
                item.get(
                    "expected_calibration_error",
                    item.get("calibration_error", 0.0),
                )
                or 0.0
            ),
            "interval_method": str(item.get("interval_method") or ""),
            "interval_status": str(item.get("interval_status") or ""),
            "confidence_level": float(item.get("confidence_level") or 0.0),
            "pareto": policy_id in pareto_ids,
        }
        # Recommendation is data, not a presentation default.
        if item.get("recommended") is not None:
            normalized["recommended"] = bool(item["recommended"])
        if item.get("status"):
            normalized["status"] = str(item["status"])
        policies.append(normalized)

    models = []
    model_ids = []
    for item in raw.get("models") or []:
        if isinstance(item, str):
            model_id = item
            name = _humanize(item)
        elif isinstance(item, dict):
            model_id = str(
                item.get("candidate_id")
                or item.get("model_id")
                or item.get("id")
                or ""
            )
            name = str(item.get("name") or _humanize(model_id))
        else:
            continue
        if model_id:
            model_ids.append(model_id)
            models.append({"id": model_id, "name": name})

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw.get("routing_matrix") or []:
        if not isinstance(row, dict):
            continue
        policy_id = str(row.get("policy_id") or "policy")
        task_class = str(row.get("task_class") or "all")
        key = (policy_id, task_class)
        grouped.setdefault(
            key,
            {
                "policy_id": policy_id,
                "task_class": f"{_humanize(policy_id)} · {_humanize(task_class)}",
                "weights": {model_id: 0.0 for model_id in model_ids},
            },
        )
        model_id = str(row.get("model_id") or "")
        if model_id:
            grouped[key]["weights"][model_id] = float(row.get("share") or 0.0)

    comparisons = []
    for item in raw.get("comparisons") or []:
        if not isinstance(item, dict):
            continue
        normalized = copy.deepcopy(item)
        if "confidence_interval" in normalized and "ci" not in normalized:
            normalized["ci"] = list(normalized["confidence_interval"])
        comparisons.append(normalized)

    return {
        "data_kind": str(raw.get("data_kind") or ""),
        "seed": raw.get("seed"),
        "description": str(raw.get("description") or ""),
        "models": models,
        "policies": policies,
        "comparisons": comparisons,
        "routing_matrix": list(grouped.values()),
        "recommendation": copy.deepcopy(raw.get("recommendation") or {}),
        "provenance": copy.deepcopy(raw.get("provenance") or {}),
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_ms(start: Any, end: Any = None) -> int:
    first = _parse_time(start)
    if first is None:
        return 0
    last = _parse_time(end) or datetime.now(timezone.utc)
    return max(0, int((last - first).total_seconds() * 1000))


def _output_summary(output: Any) -> str:
    if isinstance(output, dict):
        for key in ("summary", "result_summary", "message", "error"):
            if output.get(key):
                return str(output[key])
        if output.get("selected_candidate_id"):
            return f"Selected {output['selected_candidate_id']}"
        return json.dumps(output, ensure_ascii=False, sort_keys=True)[:300]
    if output is None:
        return ""
    return str(output)[:300]


def _event_message(event: dict[str, Any]) -> str:
    kind = str(event.get("type") or "event")
    node = str(event.get("node_id") or "")
    edge = str(event.get("edge_id") or "")
    error = str(event.get("error") or "")
    messages = {
        "run_started": "Graph run started.",
        "run_completed": "Graph run completed.",
        "run_failed": f"Graph run failed: {error}" if error else "Graph run failed.",
        "run_stopped": "Graph run stopped.",
        "run_suspended": f"{node} is awaiting input.",
        "run_resumed": f"{node} resumed.",
        "node_activated": f"{node} became ready.",
        "node_started": f"{node} started.",
        "node_succeeded": f"{node} completed.",
        "node_failed": f"{node} failed: {error}" if error else f"{node} failed.",
        "node_waiting": f"{node} is waiting.",
        "node_retry_scheduled": f"{node} scheduled a retry.",
        "edge_traversed": f"{edge} routed work to {event.get('target') or 'the next node'}.",
        "budget_exceeded": "The graph exhausted its execution budget.",
        "run_deadlocked": "The graph stopped at an unsatisfied join.",
    }
    return messages.get(kind, kind.replace("_", " ").capitalize() + ".")


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("type") or event.get("kind") or "event")
    severity = "info"
    if "fail" in kind or "error" in kind or "deadlock" in kind or "budget" in kind:
        severity = "error"
    elif kind in {"run_completed", "node_succeeded"}:
        severity = "success"
    return {
        "id": str(event.get("event_id") or f"event-{event.get('seq', 0)}"),
        "seq": int(event.get("seq") or 0),
        "timestamp": event.get("ts") or event.get("timestamp"),
        "kind": kind,
        "source": str(
            event.get("node_id")
            or event.get("edge_id")
            or event.get("source")
            or "runtime"
        ),
        "message": _event_message(event),
        "severity": severity,
    }


class GraphStudioController:
    """Own the durable demo run and project it into the browser contract."""

    def __init__(
        self,
        state_dir: Path,
        *,
        repo_root: Path | None = None,
        workflow_path: Path | None = None,
        benchmark_path: Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = Path(repo_root or _repo_root()).resolve()
        self.definition, self._workflow_raw, benchmark = load_demo_assets(
            self.repo_root,
            workflow_path=workflow_path,
            benchmark_path=benchmark_path,
        )
        self.benchmark = normalize_benchmark(benchmark)
        self.control_token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._pointer_path = (
            self.state_dir
            / "orchestration"
            / "graph_studio"
            / "current_run.json"
        )
        register_builtin_executors()
        self.run_id = self._recover_run()
        if not self.run_id:
            self.reset()

    def _is_managed(self, run: dict[str, Any]) -> bool:
        metadata = run.get("metadata")
        return (
            run.get("graph_id") == self.definition.graph_id
            and isinstance(metadata, dict)
            and metadata.get("managed_by") == _DEMO_TAG
            and metadata.get("demo_graph_id") == self.definition.graph_id
        )

    def _pointer_run_id(self) -> str:
        try:
            value = _read_json(self._pointer_path)
        except ValueError:
            return ""
        return str(value.get("run_id") or "")

    def _write_pointer(self, run_id: str) -> None:
        _atomic_write_json(
            self._pointer_path,
            {
                "run_id": run_id,
                "graph_id": self.definition.graph_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _matching_runs(self) -> list[dict[str, Any]]:
        return [
            run
            for run in list_runs(self.state_dir, include_definition=False)
            if self._is_managed(run)
        ]

    def _stop_stale(self, keep_run_id: str = "") -> None:
        for run in self._matching_runs():
            if run["run_id"] == keep_run_id:
                continue
            if run.get("status") in _ACTIVE_RUN_STATUSES:
                stop_run(
                    self.state_dir,
                    run["run_id"],
                    "superseded by the current Graph Studio run",
                )

    def _recover_run(self) -> str:
        matches = self._matching_runs()
        if not matches:
            return ""
        by_id = {run["run_id"]: run for run in matches}
        pointer_id = self._pointer_run_id()
        selected = by_id.get(pointer_id)
        if selected is None:
            selected = max(
                matches,
                key=lambda item: (
                    str(item.get("updated_at") or ""),
                    str(item.get("created_at") or ""),
                    str(item.get("run_id") or ""),
                ),
            )
        run_id = str(selected["run_id"])
        self._stop_stale(run_id)
        self._write_pointer(run_id)
        return run_id

    def _new_run(self) -> dict[str, Any]:
        run = start_run(
            self.state_dir,
            self.definition,
            metadata={
                "managed_by": _DEMO_TAG,
                "demo_graph_id": self.definition.graph_id,
            },
        )
        self.run_id = str(run["run_id"])
        self._write_pointer(self.run_id)
        return run

    def reset(self) -> dict[str, Any]:
        """Replace the managed run and advance through intake and planning."""
        with self._lock:
            if getattr(self, "run_id", ""):
                current = get_run(
                    self.state_dir, self.run_id, include_definition=False
                )
                if current and current.get("status") in _ACTIVE_RUN_STATUSES:
                    stop_run(self.state_dir, self.run_id, "demo reset")
            self._stop_stale()
            self._new_run()
            for _ in range(2):
                run = get_run(
                    self.state_dir, self.run_id, include_definition=False
                )
                if not run or run.get("status") != "running":
                    break
                tick_run(self.state_dir, self.run_id)
            return self.snapshot()

    def tick(self) -> dict[str, Any]:
        with self._lock:
            run = get_run(
                self.state_dir, self.run_id, include_definition=False
            )
            if run and run.get("status") == "running":
                tick_run(self.state_dir, self.run_id)
            return self.snapshot()

    def run_to_terminal(self, *, max_ticks: int = 128) -> dict[str, Any]:
        with self._lock:
            limit = max(1, min(int(max_ticks), _MAX_RUN_TICKS))
            run = get_run(
                self.state_dir, self.run_id, include_definition=False
            )
            if run and run.get("status") in _TERMINAL_RUN_STATUSES:
                self.reset()
            for _ in range(limit):
                run = get_run(
                    self.state_dir, self.run_id, include_definition=False
                )
                if not run or run.get("status") != "running":
                    break
                tick_run(self.state_dir, self.run_id)
            return self.snapshot()

    def run(self, *, max_ticks: int = 128) -> dict[str, Any]:
        """Compatibility-friendly name for driving the demo to a terminal state."""
        return self.run_to_terminal(max_ticks=max_ticks)

    def _events(self) -> list[dict[str, Any]]:
        path = (
            self.state_dir
            / "orchestration"
            / "graph_runs"
            / self.run_id
            / "events.jsonl"
        )
        events = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        # File order is the append order and therefore the canonical chronology.
        for line in lines[-_MAX_EVENTS:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(_normalize_event(event))
        return events

    def _definition_snapshot(self) -> dict[str, Any]:
        nodes = []
        for node in self.definition.nodes:
            raw_type = str(node.metadata.get("type") or "agent").lower()
            display_type = _TYPE_MAP.get(raw_type, raw_type)
            if node.handler == "route":
                display_type = "router"
            if display_type not in _ALLOWED_UI_TYPES:
                display_type = "agent"
            position = node.metadata.get("position")
            if not isinstance(position, dict):
                position = {}
            nodes.append(
                {
                    "id": node.id,
                    "label": node.title or node.id,
                    "type": display_type,
                    "description": str(node.metadata.get("description") or ""),
                    "position": {
                        "x": float(position.get("x") or 0.0),
                        "y": float(position.get("y") or 0.0),
                    },
                    "config": copy.deepcopy(node.config),
                    "handler": node.handler,
                    "join": node.join,
                }
            )
        edges = [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "on": edge.on,
                "label": edge.title or edge.route or edge.on,
                "route": edge.route,
            }
            for edge in self.definition.edges
        ]
        return {
            "graph_id": self.definition.graph_id,
            "name": self.definition.title or self.definition.graph_id,
            "nodes": nodes,
            "edges": edges,
        }

    def _run_snapshot(
        self, run: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        states = {}
        active_nodes = []
        for node_id, raw in run.get("node_states", {}).items():
            state = copy.deepcopy(raw) if isinstance(raw, dict) else {"status": raw}
            status = str(state.get("status") or "pending")
            if node_id in (run.get("active_nodes") or []) and status in {
                "active",
                "running",
            }:
                active_nodes.append(node_id)
            output = state.get("output")
            states[node_id] = {
                **state,
                "status": status,
                "attempt": max(
                    int(state.get("attempt") or 0),
                    int(state.get("calls") or 0),
                    int(state.get("visits") or 0),
                ),
                "duration_ms": _duration_ms(
                    state.get("started_at"), state.get("completed_at")
                ),
                "output": copy.deepcopy(output),
                "output_summary": _output_summary(output),
            }

        quality_scores = []
        route_costs = []
        route_latencies = []
        for state in states.values():
            output = state.get("output")
            if not isinstance(output, dict):
                continue
            if isinstance(output.get("score"), (int, float)):
                quality_scores.append(float(output["score"]))
            selected = output.get("selected_candidate_id")
            candidates = output.get("candidates")
            if selected and isinstance(candidates, list):
                rationale = next(
                    (
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and item.get("candidate_id") == selected
                    ),
                    None,
                )
                estimate = rationale.get("estimate") if isinstance(rationale, dict) else None
                if isinstance(estimate, dict):
                    route_costs.append(float(estimate.get("cost_usd") or 0.0))
                    route_latencies.append(
                        float(
                            estimate.get("slo_latency_ms")
                            or estimate.get("expected_latency_ms")
                            or 0.0
                        )
                    )

        completed = sum(
            state.get("status") in {"succeeded", "completed", "done"}
            for state in states.values()
        )
        retries = sum(
            event.get("kind") == "node_retry_scheduled" for event in events
        )
        metrics = {
            "quality_score": max(quality_scores, default=0.0),
            "success_rate": completed / max(1, len(states)),
            "elapsed_ms": _duration_ms(
                run.get("created_at"), run.get("completed_at")
            ),
            "cost_usd": sum(route_costs),
            "retries": retries,
            "latency_p95_ms": max(route_latencies, default=0.0),
            "run_steps": int(run.get("run_steps") or 0),
        }
        return {
            "run_id": run["run_id"],
            "status": run.get("status") or "ready",
            "active_nodes": active_nodes,
            "node_states": states,
            "started_at": run.get("created_at"),
            "metrics": metrics,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            run = get_run(
                self.state_dir, self.run_id, include_definition=False
            )
            if run is None:
                raise RuntimeError(f"managed graph run {self.run_id!r} is missing")
            events = self._events()
            return {
                "definition": self._definition_snapshot(),
                "run": self._run_snapshot(run, events),
                "events": events,
                "benchmark": copy.deepcopy(self.benchmark),
                "control_token": self.control_token,
            }

    def health(self) -> dict[str, Any]:
        run = get_run(self.state_dir, self.run_id, include_definition=False)
        return {
            "ok": run is not None,
            "service": "charon-graph-studio",
            "graph_id": self.definition.graph_id,
            "run_id": self.run_id,
            "status": run.get("status") if run else "missing",
        }


def _loopback_name(hostname: str | None) -> bool:
    if not hostname:
        return False
    value = hostname.strip().lower().rstrip(".")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _require_loopback_bind(host: str) -> None:
    if not _loopback_name(host):
        raise ValueError("Graph Studio may only bind to a loopback address")


class _GraphStudioHandler(BaseHTTPRequestHandler):
    server_version = "CharonGraphStudio/1"
    protocol_version = "HTTP/1.1"

    @property
    def controller(self) -> GraphStudioController:
        return self.server.controller  # type: ignore[attr-defined]

    @property
    def static_root(self) -> Path:
        return self.server.static_root  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(
        self, status: int, payload: dict[str, Any], *, head_only: bool = False
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if not head_only:
            self.wfile.write(raw)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _request_is_local(self) -> bool:
        return _loopback_name(str(self.client_address[0]))

    def _host_is_valid(self) -> bool:
        raw = str(self.headers.get("Host") or "")
        if not raw:
            return False
        try:
            parsed = urllib.parse.urlsplit("//" + raw)
            port = parsed.port
        except ValueError:
            return False
        if not _loopback_name(parsed.hostname):
            return False
        return port is None or port == int(self.server.server_port)

    def _origin_is_valid(self) -> bool:
        raw = self.headers.get("Origin")
        if raw is None:
            return True
        try:
            parsed = urllib.parse.urlsplit(raw)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and _loopback_name(parsed.hostname)
            and (port or 80) == int(self.server.server_port)
        )

    def _validate_request_origin(self) -> bool:
        if not self._request_is_local():
            self._error(
                HTTPStatus.FORBIDDEN,
                "loopback_required",
                "Graph Studio accepts loopback clients only.",
            )
            return False
        if not self._host_is_valid():
            self._error(
                HTTPStatus.FORBIDDEN,
                "invalid_host",
                "The Host header is not an allowed loopback origin.",
            )
            return False
        if not self._origin_is_valid():
            self._error(
                HTTPStatus.FORBIDDEN,
                "invalid_origin",
                "Cross-origin control requests are not allowed.",
            )
            return False
        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        content_type = str(self.headers.get("Content-Type") or "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "json_required",
                "Control requests require application/json.",
            )
            return None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_length", "Invalid Content-Length.")
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                "Request body exceeds the control-plane limit.",
            )
            return None
        body = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "Malformed JSON body.")
            return None
        if not isinstance(value, dict):
            self._error(
                HTTPStatus.BAD_REQUEST,
                "object_required",
                "JSON body must be an object.",
            )
            return None
        return value

    def _authorized_control(self) -> bool:
        supplied = str(self.headers.get(CONTROL_HEADER) or "")
        if not supplied or not secrets.compare_digest(
            supplied, self.controller.control_token
        ):
            self._error(
                HTTPStatus.FORBIDDEN,
                "invalid_control_token",
                "A valid Graph Studio control token is required.",
            )
            return False
        return True

    def _serve_static(self, path: str, *, head_only: bool = False) -> None:
        decoded = urllib.parse.unquote(path)
        relative = "index.html" if decoded in {"", "/"} else decoded.lstrip("/")
        parts = PurePosixPath(relative).parts
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or "\x00" in relative
            or "\\" in relative
        ):
            self._error(HTTPStatus.FORBIDDEN, "invalid_path", "Invalid static path.")
            return
        candidate = (self.static_root / Path(*parts)).resolve()
        try:
            candidate.relative_to(self.static_root)
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "path_escape", "Static path escapes root.")
            return
        if not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Static asset not found.")
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _handle_get(self, *, head_only: bool = False) -> None:
        if not self._validate_request_origin():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/health":
                self._json(
                    HTTPStatus.OK, self.controller.health(), head_only=head_only
                )
            elif path == "/api/snapshot":
                self._json(
                    HTTPStatus.OK, self.controller.snapshot(), head_only=head_only
                )
            elif path.startswith("/api/"):
                self._error(HTTPStatus.NOT_FOUND, "not_found", "API route not found.")
            else:
                self._serve_static(path, head_only=head_only)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "The Graph Studio request could not be completed.",
            )

    def do_GET(self) -> None:
        self._handle_get()

    def do_HEAD(self) -> None:
        self._handle_get(head_only=True)

    def do_POST(self) -> None:
        if not self._validate_request_origin():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in {
            "/api/demo/reset",
            "/api/demo/tick",
            "/api/demo/run",
        }:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "API route not found.")
            return
        if not self._authorized_control():
            return
        body = self._read_json_body()
        if body is None:
            return
        requested_graph = body.get("graph_id")
        if requested_graph and requested_graph != self.controller.definition.graph_id:
            self._error(
                HTTPStatus.CONFLICT,
                "graph_mismatch",
                "The requested graph does not match the managed graph.",
            )
            return
        requested_run = body.get("run_id")
        if (
            path != "/api/demo/reset"
            and requested_run
            and requested_run != self.controller.run_id
        ):
            self._error(
                HTTPStatus.CONFLICT,
                "run_mismatch",
                "The requested run is no longer current.",
            )
            return
        try:
            if path.endswith("/reset"):
                payload = self.controller.reset()
            elif path.endswith("/tick"):
                payload = self.controller.tick()
            else:
                payload = self.controller.run_to_terminal(
                    max_ticks=int(body.get("max_ticks") or 128)
                )
            self._json(HTTPStatus.OK, payload)
        except (TypeError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "The Graph Studio action could not be completed.",
            )

    def do_OPTIONS(self) -> None:
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "Cross-origin preflight requests are not supported.",
        )


class GraphStudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        controller: GraphStudioController,
        static_root: Path,
    ) -> None:
        self.controller = controller
        self.static_root = Path(static_root).resolve()
        super().__init__(address, _GraphStudioHandler)


def create_server(
    controller: GraphStudioController,
    *,
    host: str = "127.0.0.1",
    port: int = 4317,
    static_root: Path | None = None,
) -> GraphStudioHTTPServer:
    _require_loopback_bind(host)
    root = Path(
        static_root or controller.repo_root / "apps" / "graph_studio"
    ).resolve()
    if not root.is_dir():
        raise ValueError(f"Graph Studio static root does not exist: {root}")
    return GraphStudioHTTPServer((host, int(port)), controller, root)


def serve(
    state_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 4317,
    repo_root: Path | None = None,
) -> None:
    controller = GraphStudioController(state_dir, repo_root=repo_root)
    server = create_server(controller, host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


__all__ = [
    "CONTROL_HEADER",
    "GraphStudioController",
    "GraphStudioHTTPServer",
    "create_server",
    "load_demo_assets",
    "normalize_benchmark",
    "serve",
    "validate_demo_assets",
]
