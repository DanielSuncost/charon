"""Built-in executors for the durable graph runtime."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from charon.orchestration.graph_runtime import (
    ExecutorResult,
    NodeContext,
    register_executor,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAX_CANDIDATE_SOURCE_BYTES = 4 * 1024 * 1024


def _path_get(value: Any, path: str, default: Any = None) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _load_candidate_source(source: Any) -> list[dict[str, Any]]:
    raw_path = Path(str(source or "").strip())
    if not str(raw_path):
        raise ValueError("route candidate_source must not be empty")
    search = [raw_path] if raw_path.is_absolute() else [
        Path.cwd() / raw_path,
        _REPO_ROOT / raw_path,
    ]
    path = next((candidate.resolve() for candidate in search if candidate.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"route candidate_source not found: {raw_path}")
    if path.stat().st_size > _MAX_CANDIDATE_SOURCE_BYTES:
        raise ValueError("route candidate_source exceeds the 4 MiB limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("route candidate_source must contain a non-empty model list")
    if not all(isinstance(candidate, dict) for candidate in candidates):
        raise ValueError("route candidate_source models must be objects")
    return copy.deepcopy(candidates)


def noop(ctx: NodeContext) -> ExecutorResult:
    """Complete immediately with optional static output, updates, and route."""
    return ExecutorResult.success(
        copy.deepcopy(ctx.config.get("output")),
        route=ctx.config.get("route"),
        state_updates=copy.deepcopy(ctx.config.get("state_updates") or {}),
    )


def fixture(ctx: NodeContext) -> ExecutorResult | dict[str, Any]:
    """Deterministic scripted executor useful for examples and benchmarks.

    ``sequence`` entries are selected by durable call number; the final entry
    is repeated when the sequence is exhausted.
    """
    outcome_sequence = ctx.config.get("outcome_sequence")
    if outcome_sequence is not None:
        if not isinstance(outcome_sequence, list) or not outcome_sequence:
            raise ValueError("fixture outcome_sequence must be a non-empty list")
        outcome = outcome_sequence[
            min(ctx.visit - 1, len(outcome_sequence) - 1)
        ]
        output = {
            "outcome": outcome,
            "summary": str(ctx.config.get("summary") or ""),
        }
        model_from = ctx.config.get("model_from")
        if model_from:
            selected = _path_get(ctx.state, str(model_from))
            output["model"] = (
                selected.get("selected_candidate_id")
                if isinstance(selected, dict)
                else selected
            )
        state_key = str(ctx.config.get("state_key") or ctx.node_id)
        return ExecutorResult.success(
            output,
            route=str(outcome),
            state_updates={state_key: output},
        )
    if "outcome" in ctx.config:
        outcome = ctx.config["outcome"]
        output = {
            "outcome": outcome,
            "summary": str(ctx.config.get("summary") or ""),
        }
        model_from = ctx.config.get("model_from")
        if model_from:
            selected = _path_get(ctx.state, str(model_from))
            output["model"] = (
                selected.get("selected_candidate_id")
                if isinstance(selected, dict)
                else selected
            )
        state_key = str(ctx.config.get("state_key") or ctx.node_id)
        return ExecutorResult.success(
            output,
            route=str(outcome),
            state_updates={state_key: output},
        )

    sequence = ctx.config.get("sequence")
    if sequence is not None:
        if not isinstance(sequence, list) or not sequence:
            raise ValueError("fixture sequence must be a non-empty list")
        item = sequence[min(ctx.call - 1, len(sequence) - 1)]
    else:
        item = ctx.config.get("result", ctx.config)
    if not isinstance(item, dict):
        return ExecutorResult.success(copy.deepcopy(item))
    if "raise" in item:
        raise RuntimeError(str(item["raise"]))
    allowed = {
        "status",
        "action",
        "output",
        "state_updates",
        "route",
        "delay_sec",
        "reason",
        "resume_key",
        "error",
        "retryable",
    }
    return {key: copy.deepcopy(value) for key, value in item.items() if key in allowed}


def _route_endpoints_match(
    selected: dict[str, Any] | None,
    executed: dict[str, Any] | None,
) -> bool:
    if not isinstance(selected, dict) or not isinstance(executed, dict):
        return False
    for key in ("provider", "model_id", "context_window"):
        if selected.get(key) != executed.get(key):
            return False
    selected_base = str(selected.get("base_url") or "").rstrip("/")
    executed_base = str(executed.get("base_url") or "").rstrip("/")
    return not selected_base or selected_base == executed_base


def route(ctx: NodeContext) -> ExecutorResult:
    """Resolve a model route lazily and expose the selection as node output."""
    candidates = ctx.config.get("candidates")
    candidate_source = ctx.config.get("candidate_source")
    if candidates is None and candidate_source:
        candidates = _load_candidate_source(candidate_source)
    if candidates is not None:
        from charon.routing import route_models

        decision = route_models(
            ctx.config.get("task") or {},
            candidates,
            policy=ctx.config.get("policy") or "balanced",
        )
        output = decision.to_dict()
        selected = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("candidate_id") or candidate.get("id") or "")
                == decision.selected_candidate_id
            ),
            None,
        )
        if selected is not None:
            endpoint = {
                "candidate_id": decision.selected_candidate_id,
                "provider": str(selected.get("provider") or ""),
                "model_id": str(selected.get("model_id") or selected.get("model") or ""),
                "context_window": int(selected.get("context_window") or 0),
            }
            metadata = selected.get("metadata")
            base_url = selected.get("base_url")
            if base_url is None and isinstance(metadata, dict):
                base_url = metadata.get("base_url")
            if base_url:
                endpoint["base_url"] = str(base_url)
            output["selected_endpoint"] = endpoint
        if candidate_source:
            output["candidate_source"] = str(candidate_source)
        label = (
            decision.selected_candidate_id
            or ctx.config.get("unavailable_route", "unavailable")
        )
        state_key = str(ctx.config.get("state_key") or ctx.node_id)
        return ExecutorResult.success(
            output,
            route=str(label),
            state_updates={state_key: output},
        )

    static_label = ctx.config.get("label")
    if static_label is not None:
        return ExecutorResult.success(
            {"label": str(static_label), "source": "static"},
            route=str(static_label),
        )

    # Importing provider construction can initialize credentials and clients,
    # so it is intentionally deferred until this node actually executes.
    from charon.providers.model_registry import get_shade_provider_and_model

    provider, model, ready = get_shade_provider_and_model(
        ctx.state_dir,
        phase_name=str(ctx.config.get("phase_name") or ""),
        task_complexity=str(ctx.config.get("task_complexity") or "normal"),
    )
    from charon.providers.provider_bridge import describe_provider_endpoint

    endpoint = describe_provider_endpoint(provider, model)
    endpoint.update(
        {
            "resolver": "model_registry",
            "phase_name": str(ctx.config.get("phase_name") or ""),
            "task_complexity": str(
                ctx.config.get("task_complexity") or "normal"
            ),
        }
    )
    selected_id = str(endpoint.get("model_id") or "")
    selection = {
        "task_id": str(ctx.config.get("task_id") or ctx.node_id),
        "policy": "provider-registry",
        "policy_version": "provider-registry-v1",
        "policy_weights": {},
        "selected_candidate_id": selected_id if ready else None,
        "feasible": bool(ready),
        "selection_reason": (
            "Resolved the configured provider registry endpoint."
            if ready
            else "The configured provider registry endpoint is unavailable."
        ),
        "candidates": [],
        "ready": bool(ready),
        "phase_name": str(ctx.config.get("phase_name") or ""),
        "task_complexity": str(ctx.config.get("task_complexity") or "normal"),
    }
    if ready:
        selection["selected_endpoint"] = endpoint
    label = (
        ctx.config.get("ready_route", "ready")
        if ready
        else ctx.config.get("unavailable_route", "unavailable")
    )
    state_key = str(ctx.config.get("state_key") or "model_route")
    return ExecutorResult.success(
        selection,
        route=str(label),
        state_updates={state_key: selection},
    )


def queue_agent(ctx: NodeContext) -> ExecutorResult:
    """Dispatch one legacy agent task exactly once, then poll it to completion."""
    from charon.conversation.conversation_runtime import (
        enqueue_agent_task,
        load_queue,
    )

    owner = str(ctx.config.get("owner_agent_id") or "").strip()
    instruction = str(ctx.config.get("instruction") or "").strip()
    if not owner:
        return ExecutorResult.failure("queue_agent requires owner_agent_id")
    if not instruction:
        return ExecutorResult.failure("queue_agent requires instruction")

    correlation_id = ctx.idempotency_key
    route_from = str(ctx.config.get("route_from") or "").strip()
    routing_decision = (
        _path_get(ctx.state, route_from)
        if route_from
        else copy.deepcopy(ctx.config.get("routing_decision"))
    )
    model_route = copy.deepcopy(ctx.config.get("model_route"))
    if isinstance(routing_decision, dict):
        model_route = copy.deepcopy(routing_decision.get("selected_endpoint"))
        if model_route is None and routing_decision.get("provider"):
            model_route = {
                "candidate_id": str(
                    routing_decision.get("candidate_id") or ""
                ),
                "provider": str(routing_decision["provider"]),
                "model_id": str(
                    routing_decision.get("model_id")
                    or routing_decision.get("model")
                    or ""
                ),
            }
            if routing_decision.get("context_window") is not None:
                model_route["context_window"] = routing_decision[
                    "context_window"
                ]
            if routing_decision.get("base_url"):
                model_route["base_url"] = routing_decision["base_url"]
            routing_decision = {
                **routing_decision,
                "selected_endpoint": copy.deepcopy(model_route),
            }
        if model_route is None:
            return ExecutorResult.failure(
                "queue_agent route_from did not resolve selected_endpoint"
            )
    if model_route is not None and not isinstance(model_route, dict):
        return ExecutorResult.failure("queue_agent model_route must be an object")
    queue = load_queue(ctx.state_dir)
    task = next(
        (
            item
            for item in queue
            if str(item.get("correlation_id") or "") == correlation_id
        ),
        None,
    )
    if task is None:
        task = enqueue_agent_task(
            ctx.state_dir,
            owner_agent_id=owner,
            instruction=instruction,
            title=ctx.config.get("title"),
            project=ctx.config.get("project"),
            priority=str(ctx.config.get("priority") or "normal"),
            scope=list(ctx.config.get("scope") or []),
            deps=list(ctx.config.get("deps") or []),
            correlation_id=correlation_id,
            max_attempts=int(ctx.config.get("max_attempts") or 3),
            model_route=model_route,
            routing_decision=(
                routing_decision if isinstance(routing_decision, dict) else None
            ),
        )

    status = str(task.get("status") or "pending").lower()
    selected_endpoint = (
        task.get("selected_endpoint")
        or task.get("selected_model")
        or task.get("model_route")
    )
    executed_endpoint = (
        task.get("executed_endpoint")
        or task.get("executed_model")
    )
    output = {
        "task_id": task.get("id"),
        "correlation_id": correlation_id,
        "status": status,
        "result_summary": task.get("result_summary"),
        "error": task.get("error"),
        "selected_model": copy.deepcopy(selected_endpoint),
        "executed_model": copy.deepcopy(executed_endpoint),
    }
    if selected_endpoint is not None or executed_endpoint is not None:
        route_honored = task.get("route_honored")
        output["route_honored"] = (
            route_honored
            if isinstance(route_honored, bool)
            else _route_endpoints_match(
                selected_endpoint,
                executed_endpoint,
            )
        )
    if status in {"completed", "done", "succeeded", "success"}:
        return ExecutorResult.success(output)
    if status in {"failed", "cancelled", "canceled", "stopped"}:
        return ExecutorResult.failure(
            str(task.get("error") or task.get("result_summary") or f"task {status}"),
            output=output,
        )
    return ExecutorResult.wait(
        delay_sec=float(ctx.config.get("poll_delay_sec") or 1.0),
        output=output,
    )


def quality_gate(ctx: NodeContext) -> ExecutorResult:
    """Route by a deterministic threshold or required-state check."""
    config = ctx.config
    missing = [
        key
        for key in config.get("required_keys", [])
        if _path_get(ctx.state, str(key), None) is None
    ]
    value = (
        copy.deepcopy(config["value"])
        if "value" in config
        else _path_get(ctx.state, str(config.get("state_key") or ""), None)
    )
    operator = str(config.get("operator") or "gte")
    target = config.get("threshold", config.get("expected", True))
    try:
        if missing:
            passed = False
        elif operator == "gte":
            passed = value is not None and value >= target
        elif operator == "gt":
            passed = value is not None and value > target
        elif operator == "lte":
            passed = value is not None and value <= target
        elif operator == "lt":
            passed = value is not None and value < target
        elif operator == "eq":
            passed = value == target
        elif operator == "truthy":
            passed = bool(value)
        else:
            raise ValueError(f"unknown quality_gate operator {operator!r}")
    except TypeError:
        passed = False
    label = str(
        config.get("pass_route", "pass")
        if passed
        else config.get("fail_route", "fail")
    )
    output = {
        "passed": passed,
        "value": value,
        "operator": operator,
        "target": target,
        "missing_keys": missing,
    }
    return ExecutorResult.success(output, route=label)


def human_gate(ctx: NodeContext) -> ExecutorResult:
    """Suspend until a payload is supplied through ``resume_run``."""
    if ctx.resume_payload is None:
        return ExecutorResult.suspend(
            str(ctx.config.get("prompt") or "input required"),
            resume_key=str(
                ctx.config.get("resume_key")
                or f"{ctx.run_id}:{ctx.node_id}:{ctx.visit}"
            ),
        )
    payload = copy.deepcopy(ctx.resume_payload)
    route_label = ctx.config.get("default_route")
    if isinstance(payload, dict):
        route_label = payload.get("route", route_label)
        if route_label is None and "approved" in payload:
            route_label = (
                ctx.config.get("approve_route", "approved")
                if payload["approved"]
                else ctx.config.get("reject_route", "rejected")
            )
    return ExecutorResult.success(payload, route=route_label)


_BUILTINS = {
    "noop": noop,
    "route": route,
    "queue_agent": queue_agent,
    "quality_gate": quality_gate,
    "human_gate": human_gate,
    "fixture": fixture,
}


def register_builtin_executors() -> None:
    """Idempotently install all built-in handler implementations."""
    for name, executor in _BUILTINS.items():
        register_executor(name, executor)


__all__ = [
    "fixture",
    "human_gate",
    "noop",
    "quality_gate",
    "queue_agent",
    "register_builtin_executors",
    "route",
]
