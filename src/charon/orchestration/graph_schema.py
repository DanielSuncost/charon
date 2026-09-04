"""Declarative schema for durable graph orchestration.

Definitions contain data only.  Executor functions are addressed by a stable
``handler`` name and are resolved by the runtime registry when a node runs.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar


_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_JOIN_MODES = frozenset({"any", "all"})
_EDGE_MODES = frozenset({"success", "failure", "always"})


class GraphValidationError(ValueError):
    """Raised when a graph definition is ambiguous or unsafe to persist."""


def _valid_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise GraphValidationError(
            f"{field_name} must match {_ID_RE.pattern!r}; got {value!r}"
        )
    return value


def _positive_int(value: Any, field_name: str, *, allow_none: bool = False) -> int | None:
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GraphValidationError(f"{field_name} must be a positive integer")
    return value


def _finite_nonnegative(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphValidationError(f"{field_name} must be a non-negative number")
    number = float(value)
    if number < 0 or number == float("inf") or number != number:
        raise GraphValidationError(f"{field_name} must be a finite non-negative number")
    return number


def _json_value(value: Any, field_name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise GraphValidationError(f"{field_name} must be JSON-serializable: {exc}") from exc
    return copy.deepcopy(value)


def _strict_fields(data: dict[str, Any], allowed: set[str], type_name: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise GraphValidationError(
            f"{type_name} contains unknown field(s): {', '.join(sorted(unknown))}"
        )


@dataclass(frozen=True)
class NodeSpec:
    """One executable node in a graph definition."""

    id: str
    handler: str
    title: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    join: str = "any"
    max_attempts: int = 3
    backoff_base_sec: float = 1.0
    max_visits: int | None = None
    terminal: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _valid_id(self.id, "node.id")
        _valid_id(self.handler, f"node {self.id!r} handler")
        if not isinstance(self.title, str):
            raise GraphValidationError(f"node {self.id!r} title must be a string")
        if self.join not in _JOIN_MODES:
            raise GraphValidationError(
                f"node {self.id!r} join must be one of {sorted(_JOIN_MODES)}"
            )
        _positive_int(self.max_attempts, f"node {self.id!r} max_attempts")
        _finite_nonnegative(
            self.backoff_base_sec, f"node {self.id!r} backoff_base_sec"
        )
        _positive_int(
            self.max_visits, f"node {self.id!r} max_visits", allow_none=True
        )
        if not isinstance(self.terminal, bool):
            raise GraphValidationError(f"node {self.id!r} terminal must be boolean")
        if not isinstance(self.config, dict):
            raise GraphValidationError(f"node {self.id!r} config must be an object")
        if not isinstance(self.metadata, dict):
            raise GraphValidationError(f"node {self.id!r} metadata must be an object")
        object.__setattr__(
            self, "config", _json_value(self.config, f"node {self.id!r} config")
        )
        object.__setattr__(
            self, "metadata", _json_value(self.metadata, f"node {self.id!r} metadata")
        )
        object.__setattr__(self, "backoff_base_sec", float(self.backoff_base_sec))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "handler": self.handler,
            "title": self.title,
            "config": copy.deepcopy(self.config),
            "join": self.join,
            "max_attempts": self.max_attempts,
            "backoff_base_sec": self.backoff_base_sec,
            "max_visits": self.max_visits,
            "terminal": self.terminal,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeSpec:
        if not isinstance(data, dict):
            raise GraphValidationError("NodeSpec must be an object")
        allowed = {
            "id",
            "handler",
            "title",
            "config",
            "join",
            "max_attempts",
            "backoff_base_sec",
            "max_visits",
            "terminal",
            "metadata",
        }
        _strict_fields(data, allowed, "NodeSpec")
        try:
            return cls(**data)
        except TypeError as exc:
            raise GraphValidationError(f"invalid NodeSpec: {exc}") from exc


@dataclass(frozen=True)
class EdgeSpec:
    """A directed transition selected after node success or failure."""

    id: str
    source: str
    target: str
    on: str = "success"
    route: str | None = None
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _valid_id(self.id, "edge.id")
        _valid_id(self.source, f"edge {self.id!r} source")
        _valid_id(self.target, f"edge {self.id!r} target")
        if self.on not in _EDGE_MODES:
            raise GraphValidationError(
                f"edge {self.id!r} on must be one of {sorted(_EDGE_MODES)}"
            )
        if self.route is not None:
            if not isinstance(self.route, str) or not self.route.strip():
                raise GraphValidationError(
                    f"edge {self.id!r} route must be a non-empty string or null"
                )
            if len(self.route) > 128:
                raise GraphValidationError(f"edge {self.id!r} route is too long")
        if not isinstance(self.title, str):
            raise GraphValidationError(f"edge {self.id!r} title must be a string")
        if not isinstance(self.metadata, dict):
            raise GraphValidationError(f"edge {self.id!r} metadata must be an object")
        object.__setattr__(
            self, "metadata", _json_value(self.metadata, f"edge {self.id!r} metadata")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "on": self.on,
            "route": self.route,
            "title": self.title,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EdgeSpec:
        if not isinstance(data, dict):
            raise GraphValidationError("EdgeSpec must be an object")
        allowed = {"id", "source", "target", "on", "route", "title", "metadata"}
        _strict_fields(data, allowed, "EdgeSpec")
        try:
            return cls(**data)
        except TypeError as exc:
            raise GraphValidationError(f"invalid EdgeSpec: {exc}") from exc


@dataclass(frozen=True)
class GraphDefinition:
    """A validated, JSON-serializable workflow graph."""

    SCHEMA_VERSION: ClassVar[int] = 1

    graph_id: str
    nodes: tuple[NodeSpec, ...] | list[NodeSpec]
    edges: tuple[EdgeSpec, ...] | list[EdgeSpec]
    entry_nodes: tuple[str, ...] | list[str]
    title: str = ""
    max_node_visits: int = 32
    max_run_steps: int = 1000
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _valid_id(self.graph_id, "graph_id")
        if not isinstance(self.title, str):
            raise GraphValidationError("graph title must be a string")
        _positive_int(self.max_node_visits, "max_node_visits")
        _positive_int(self.max_run_steps, "max_run_steps")
        if not isinstance(self.metadata, dict):
            raise GraphValidationError("graph metadata must be an object")

        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        entries = tuple(self.entry_nodes)
        if not nodes:
            raise GraphValidationError("a graph must contain at least one node")
        if not entries:
            raise GraphValidationError("a graph must contain at least one entry node")
        if any(not isinstance(node, NodeSpec) for node in nodes):
            raise GraphValidationError("graph nodes must be NodeSpec instances")
        if any(not isinstance(edge, EdgeSpec) for edge in edges):
            raise GraphValidationError("graph edges must be EdgeSpec instances")

        node_ids = [node.id for node in nodes]
        edge_ids = [edge.id for edge in edges]
        if len(node_ids) != len(set(node_ids)):
            raise GraphValidationError("node ids must be unique")
        if len(edge_ids) != len(set(edge_ids)):
            raise GraphValidationError("edge ids must be unique")
        if len(entries) != len(set(entries)):
            raise GraphValidationError("entry node ids must be unique")

        node_set = set(node_ids)
        missing_entries = set(entries) - node_set
        if missing_entries:
            raise GraphValidationError(
                f"entry nodes do not exist: {', '.join(sorted(missing_entries))}"
            )
        for entry in entries:
            _valid_id(entry, "entry node")
        for edge in edges:
            if edge.source not in node_set or edge.target not in node_set:
                raise GraphValidationError(
                    f"edge {edge.id!r} references missing endpoint "
                    f"{edge.source!r} -> {edge.target!r}"
                )

        adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for edge in edges:
            adjacency[edge.source].append(edge.target)
        reachable = set(entries)
        frontier = list(entries)
        while frontier:
            source = frontier.pop()
            for target in adjacency[source]:
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        unreachable = node_set - reachable
        if unreachable:
            raise GraphValidationError(
                "all nodes must be reachable from an entry node; unreachable: "
                + ", ".join(sorted(unreachable))
            )

        for node in nodes:
            if node.terminal and any(
                edge.source == node.id and edge.on in {"success", "always"}
                for edge in edges
            ):
                raise GraphValidationError(
                    f"terminal node {node.id!r} cannot have success/always edges"
                )

        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "entry_nodes", entries)
        object.__setattr__(self, "metadata", _json_value(self.metadata, "graph metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "graph_id": self.graph_id,
            "title": self.title,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "entry_nodes": list(self.entry_nodes),
            "max_node_visits": self.max_node_visits,
            "max_run_steps": self.max_run_steps,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphDefinition:
        if not isinstance(data, dict):
            raise GraphValidationError("GraphDefinition must be an object")
        allowed = {
            "schema_version",
            "graph_id",
            "title",
            "nodes",
            "edges",
            "entry_nodes",
            "max_node_visits",
            "max_run_steps",
            "metadata",
        }
        _strict_fields(data, allowed, "GraphDefinition")
        version = data.get("schema_version", cls.SCHEMA_VERSION)
        if version != cls.SCHEMA_VERSION:
            raise GraphValidationError(
                f"unsupported graph schema_version {version!r}"
            )
        values = dict(data)
        values.pop("schema_version", None)
        try:
            values["nodes"] = [
                node if isinstance(node, NodeSpec) else NodeSpec.from_dict(node)
                for node in values.get("nodes", [])
            ]
            values["edges"] = [
                edge if isinstance(edge, EdgeSpec) else EdgeSpec.from_dict(edge)
                for edge in values.get("edges", [])
            ]
            return cls(**values)
        except TypeError as exc:
            raise GraphValidationError(f"invalid GraphDefinition: {exc}") from exc


__all__ = [
    "EdgeSpec",
    "GraphDefinition",
    "GraphValidationError",
    "NodeSpec",
]
