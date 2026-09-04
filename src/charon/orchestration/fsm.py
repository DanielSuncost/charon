"""Small, pure finite-state-machine primitives for durable coordinators.

The kernel deliberately does not own persistence.  ``dispatch`` returns a new
``MachineInstance`` whose transition event is staged in ``event_outbox``.  A
caller can therefore persist its domain snapshot and the staged event in one
atomic document, then publish/acknowledge the outbox using the storage
transaction it already trusts.

Instances are treated as immutable values: dispatch never mutates the supplied
instance.  Stable caller-provided event IDs make replay idempotent, while an
optional expected revision provides optimistic concurrency for stores that do
not already serialize writers with a lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Union


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class FSMError(RuntimeError):
    """Base class for runtime machine errors."""


class MachineDefinitionError(ValueError):
    """Raised when a machine specification is ambiguous or invalid."""


class TransitionRejected(FSMError):
    """Raised when no legal, guard-approved transition can handle an event."""


class RevisionConflict(FSMError):
    """Raised when optimistic concurrency observes a stale revision."""


class InvariantViolation(FSMError):
    """Raised when persisted or candidate machine state violates an invariant."""


class EventCollision(FSMError):
    """Raised when an event ID is reused for different event content."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise MachineDefinitionError(
            f"{field_name} must match {_NAME_RE.pattern!r}; got {value!r}"
        )
    return value


def _json_copy(value: Any, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serializable: {exc}") from exc
    return json.loads(encoded)


def _fingerprint(event: str, payload: Any) -> str:
    encoded = json.dumps(
        {"event": event, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TransitionContext:
    """Read-only inputs supplied to guards, reducers, and invariants."""

    machine: str
    machine_version: int
    instance_id: str
    event_id: str
    event: str
    source: str
    target: str
    revision: int
    data: dict[str, Any]
    payload: Any = None


Guard = Callable[[TransitionContext], Union[bool, str, None]]
Reducer = Callable[[TransitionContext], Optional[Mapping[str, Any]]]
Invariant = Callable[[TransitionContext], Union[bool, str, None]]


@dataclass(frozen=True)
class TransitionSpec:
    """One event-triggered transition from one or more source states.

    ``guard`` may return ``False`` or a reason string to reject the event.
    ``reducer`` returns a shallow patch for the instance's JSON data.  Reducers
    receive defensive copies and cannot mutate the current instance in place.
    """

    event: str
    sources: str | Iterable[str]
    target: str
    guard: Guard | None = None
    reducer: Reducer | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _name(self.event, "transition event")
        _name(self.target, f"transition {self.event!r} target")
        raw_sources = (self.sources,) if isinstance(self.sources, str) else tuple(self.sources)
        if not raw_sources:
            raise MachineDefinitionError(
                f"transition {self.event!r} must have at least one source"
            )
        sources = frozenset(
            _name(source, f"transition {self.event!r} source")
            for source in raw_sources
        )
        if self.guard is not None and not callable(self.guard):
            raise MachineDefinitionError(
                f"transition {self.event!r} guard must be callable"
            )
        if self.reducer is not None and not callable(self.reducer):
            raise MachineDefinitionError(
                f"transition {self.event!r} reducer must be callable"
            )
        if not isinstance(self.metadata, dict):
            raise MachineDefinitionError(
                f"transition {self.event!r} metadata must be an object"
            )
        object.__setattr__(self, "sources", sources)
        object.__setattr__(
            self,
            "metadata",
            _json_copy(self.metadata, f"transition {self.event!r} metadata"),
        )


@dataclass(frozen=True)
class MachineSpec:
    """Validated lifecycle definition indexed by ``(source, event)``."""

    name: str
    states: Iterable[str]
    initial_state: str
    transitions: Iterable[TransitionSpec]
    terminal_states: Iterable[str] = field(default_factory=tuple)
    invariants: Iterable[Invariant] = field(default_factory=tuple)
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    _index: dict[tuple[str, str], TransitionSpec] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _name(self.name, "machine name")
        states = frozenset(_name(state, "machine state") for state in self.states)
        if not states:
            raise MachineDefinitionError("a machine must contain at least one state")
        _name(self.initial_state, "initial state")
        if self.initial_state not in states:
            raise MachineDefinitionError(
                f"initial state {self.initial_state!r} is not declared"
            )
        terminal = frozenset(
            _name(state, "terminal state") for state in self.terminal_states
        )
        missing_terminal = terminal - states
        if missing_terminal:
            raise MachineDefinitionError(
                "terminal states are not declared: "
                + ", ".join(sorted(missing_terminal))
            )
        transitions = tuple(self.transitions)
        if any(not isinstance(item, TransitionSpec) for item in transitions):
            raise MachineDefinitionError(
                "machine transitions must be TransitionSpec instances"
            )
        index: dict[tuple[str, str], TransitionSpec] = {}
        for transition in transitions:
            missing = set(transition.sources) - states
            if transition.target not in states or missing:
                raise MachineDefinitionError(
                    f"transition {transition.event!r} references undeclared states"
                )
            illegal_terminal = set(transition.sources) & terminal
            if illegal_terminal:
                raise MachineDefinitionError(
                    f"terminal states cannot have outgoing transitions: "
                    f"{', '.join(sorted(illegal_terminal))}"
                )
            for source in transition.sources:
                key = (source, transition.event)
                if key in index:
                    raise MachineDefinitionError(
                        f"ambiguous transition for state {source!r} and "
                        f"event {transition.event!r}"
                    )
                index[key] = transition
        invariants = tuple(self.invariants)
        if any(not callable(item) for item in invariants):
            raise MachineDefinitionError("machine invariants must be callable")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise MachineDefinitionError("machine version must be a positive integer")
        if not isinstance(self.metadata, dict):
            raise MachineDefinitionError("machine metadata must be an object")
        object.__setattr__(self, "states", states)
        object.__setattr__(self, "transitions", transitions)
        object.__setattr__(self, "terminal_states", terminal)
        object.__setattr__(self, "invariants", invariants)
        object.__setattr__(self, "metadata", _json_copy(self.metadata, "machine metadata"))
        object.__setattr__(self, "_index", index)

    def transition_for(self, state: str, event: str) -> TransitionSpec | None:
        return self._index.get((state, event))

    def allowed_events(self, state: str) -> tuple[str, ...]:
        return tuple(sorted(event for source, event in self._index if source == state))


@dataclass(frozen=True)
class MachineInstance:
    """Serializable durable state for one ``MachineSpec`` instance."""

    machine: str
    machine_version: int
    instance_id: str
    state: str
    data: dict[str, Any] = field(default_factory=dict)
    revision: int = 0
    event_seq: int = 0
    processed_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    event_outbox: tuple[dict[str, Any], ...] | list[dict[str, Any]] = field(
        default_factory=tuple
    )
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        _name(self.machine, "instance machine")
        _name(self.instance_id, "instance id")
        _name(self.state, "instance state")
        if (
            isinstance(self.machine_version, bool)
            or not isinstance(self.machine_version, int)
            or self.machine_version < 1
        ):
            raise ValueError("instance machine_version must be a positive integer")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("instance revision must be a non-negative integer")
        if isinstance(self.event_seq, bool) or not isinstance(self.event_seq, int) or self.event_seq < 0:
            raise ValueError("instance event_seq must be a non-negative integer")
        if not isinstance(self.data, dict):
            raise ValueError("instance data must be an object")
        if not isinstance(self.processed_events, dict):
            raise ValueError("instance processed_events must be an object")
        if not isinstance(self.event_outbox, (tuple, list)):
            raise ValueError("instance event_outbox must be an array")
        data = _json_copy(self.data, "instance data")
        processed = _json_copy(self.processed_events, "processed events")
        outbox = tuple(_json_copy(list(self.event_outbox), "event outbox"))
        for event_id, record in processed.items():
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("processed event ids must be non-empty strings")
            if not isinstance(record, dict):
                raise ValueError("processed event records must be objects")
        if any(not isinstance(event, dict) for event in outbox):
            raise ValueError("event outbox rows must be objects")
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "processed_events", processed)
        object.__setattr__(self, "event_outbox", outbox)
        object.__setattr__(self, "created_at", self.created_at or _now_iso())
        object.__setattr__(self, "updated_at", self.updated_at or self.created_at or _now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "machine_version": self.machine_version,
            "instance_id": self.instance_id,
            "state": self.state,
            "data": copy.deepcopy(self.data),
            "revision": self.revision,
            "event_seq": self.event_seq,
            "processed_events": copy.deepcopy(self.processed_events),
            "event_outbox": copy.deepcopy(list(self.event_outbox)),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MachineInstance:
        if not isinstance(value, Mapping):
            raise ValueError("MachineInstance must be an object")
        allowed = {
            "machine",
            "machine_version",
            "instance_id",
            "state",
            "data",
            "revision",
            "event_seq",
            "processed_events",
            "event_outbox",
            "created_at",
            "updated_at",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                "MachineInstance contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ValueError(f"invalid MachineInstance: {exc}") from exc


@dataclass(frozen=True)
class DispatchResult:
    instance: MachineInstance
    event: dict[str, Any]
    duplicate: bool = False


def create_instance(
    spec: MachineSpec,
    instance_id: str,
    *,
    data: Mapping[str, Any] | None = None,
    state: str | None = None,
    now: str | None = None,
) -> MachineInstance:
    """Create a new instance, or adopt a known state during legacy migration."""
    instance = MachineInstance(
        machine=spec.name,
        machine_version=spec.version,
        instance_id=instance_id,
        state=state or spec.initial_state,
        data=dict(data or {}),
        created_at=now or _now_iso(),
        updated_at=now or _now_iso(),
    )
    validate_instance(spec, instance)
    return instance


def _evaluate(
    check: Guard | Invariant,
    context: TransitionContext,
    *,
    label: str,
    error_type: type[FSMError],
) -> None:
    try:
        answer = check(context)
    except Exception as exc:  # noqa: BLE001 - isolate domain callbacks
        raise error_type(f"{label} raised {type(exc).__name__}: {exc}") from exc
    if isinstance(answer, str):
        if answer:
            raise error_type(answer)
        return
    if answer is False:
        raise error_type(f"{label} rejected the transition")


def validate_instance(spec: MachineSpec, instance: MachineInstance) -> None:
    """Validate machine identity, state, and invariants for restored state."""
    if instance.machine != spec.name:
        raise InvariantViolation(
            f"instance belongs to machine {instance.machine!r}, not {spec.name!r}"
        )
    if instance.machine_version != spec.version:
        raise InvariantViolation(
            f"instance machine version {instance.machine_version} does not match "
            f"specification version {spec.version}"
        )
    if instance.state not in spec.states:
        raise InvariantViolation(f"unknown persisted state {instance.state!r}")
    context = TransitionContext(
        machine=spec.name,
        machine_version=spec.version,
        instance_id=instance.instance_id,
        event_id="validate",
        event="validate",
        source=instance.state,
        target=instance.state,
        revision=instance.revision,
        data=copy.deepcopy(instance.data),
        payload=None,
    )
    for index, invariant in enumerate(spec.invariants):
        _evaluate(
            invariant,
            context,
            label=f"invariant {index}",
            error_type=InvariantViolation,
        )


def dispatch(
    spec: MachineSpec,
    instance: MachineInstance,
    event: str,
    *,
    event_id: str,
    payload: Any = None,
    expected_revision: int | None = None,
    now: str | None = None,
) -> DispatchResult:
    """Apply one guarded transition and stage its audit event atomically.

    A replay with the same ``event_id`` and content returns ``duplicate=True``
    even if ``expected_revision`` is now stale.  Reusing an ID for different
    content raises ``EventCollision``.
    """
    _name(event, "event")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")
    payload_value = _json_copy(payload, "event payload")
    validate_instance(spec, instance)
    fingerprint = _fingerprint(event, payload_value)
    prior = instance.processed_events.get(event_id)
    if prior is not None:
        if prior.get("fingerprint") != fingerprint:
            raise EventCollision(
                f"event id {event_id!r} was already used for different content"
            )
        prior_event = prior.get("transition")
        if not isinstance(prior_event, dict):
            raise InvariantViolation(
                f"processed event {event_id!r} has no transition record"
            )
        return DispatchResult(
            instance=instance,
            event=copy.deepcopy(prior_event),
            duplicate=True,
        )
    if expected_revision is not None:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        if instance.revision != expected_revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {instance.revision}"
            )
    transition = spec.transition_for(instance.state, event)
    if transition is None:
        allowed = spec.allowed_events(instance.state)
        suffix = f"; allowed: {', '.join(allowed)}" if allowed else ""
        raise TransitionRejected(
            f"event {event!r} is not legal from state {instance.state!r}{suffix}"
        )
    current_context = TransitionContext(
        machine=spec.name,
        machine_version=spec.version,
        instance_id=instance.instance_id,
        event_id=event_id,
        event=event,
        source=instance.state,
        target=transition.target,
        revision=instance.revision,
        data=copy.deepcopy(instance.data),
        payload=copy.deepcopy(payload_value),
    )
    if transition.guard is not None:
        _evaluate(
            transition.guard,
            current_context,
            label=f"guard for {event!r}",
            error_type=TransitionRejected,
        )
    next_data = copy.deepcopy(instance.data)
    if transition.reducer is not None:
        try:
            patch = transition.reducer(current_context)
        except Exception as exc:  # noqa: BLE001 - isolate domain callbacks
            raise TransitionRejected(
                f"reducer for {event!r} raised {type(exc).__name__}: {exc}"
            ) from exc
        if patch is not None:
            if not isinstance(patch, Mapping):
                raise TransitionRejected(
                    f"reducer for {event!r} must return an object or null"
                )
            next_data.update(_json_copy(dict(patch), "transition data patch"))
    next_revision = instance.revision + 1
    next_seq = instance.event_seq + 1
    timestamp = now or _now_iso()
    candidate_context = TransitionContext(
        machine=spec.name,
        machine_version=spec.version,
        instance_id=instance.instance_id,
        event_id=event_id,
        event=event,
        source=instance.state,
        target=transition.target,
        revision=next_revision,
        data=copy.deepcopy(next_data),
        payload=copy.deepcopy(payload_value),
    )
    for index, invariant in enumerate(spec.invariants):
        _evaluate(
            invariant,
            candidate_context,
            label=f"invariant {index}",
            error_type=InvariantViolation,
        )
    transition_event = {
        "event_id": event_id,
        "seq": next_seq,
        "ts": timestamp,
        "kind": "state_transition",
        "type": event,
        "machine": spec.name,
        "machine_version": spec.version,
        "instance_id": instance.instance_id,
        "source": instance.state,
        "target": transition.target,
        "revision": next_revision,
        "payload": copy.deepcopy(payload_value),
        "metadata": copy.deepcopy(transition.metadata),
    }
    processed = copy.deepcopy(instance.processed_events)
    processed[event_id] = {
        "fingerprint": fingerprint,
        "transition": copy.deepcopy(transition_event),
    }
    next_instance = MachineInstance(
        machine=instance.machine,
        machine_version=instance.machine_version,
        instance_id=instance.instance_id,
        state=transition.target,
        data=next_data,
        revision=next_revision,
        event_seq=next_seq,
        processed_events=processed,
        event_outbox=(*instance.event_outbox, transition_event),
        created_at=instance.created_at,
        updated_at=timestamp,
    )
    return DispatchResult(
        instance=next_instance,
        event=copy.deepcopy(transition_event),
        duplicate=False,
    )


def acknowledge_events(
    instance: MachineInstance,
    event_ids: Iterable[str],
) -> MachineInstance:
    """Return an instance with published outbox rows removed.

    Acknowledgement does not advance the lifecycle revision: it is transport
    bookkeeping performed under the caller's persistence lock/transaction.
    """
    acknowledged = frozenset(str(event_id) for event_id in event_ids)
    if not acknowledged:
        return instance
    remaining = tuple(
        event
        for event in instance.event_outbox
        if str(event.get("event_id") or "") not in acknowledged
    )
    if len(remaining) == len(instance.event_outbox):
        return instance
    return MachineInstance(
        machine=instance.machine,
        machine_version=instance.machine_version,
        instance_id=instance.instance_id,
        state=instance.state,
        data=instance.data,
        revision=instance.revision,
        event_seq=instance.event_seq,
        processed_events=instance.processed_events,
        event_outbox=remaining,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
    )


def project_machine(
    spec: MachineSpec,
    instance: MachineInstance | None = None,
) -> dict[str, Any]:
    """Return presentation-neutral lifecycle topology and optional live state."""
    if instance is not None:
        validate_instance(spec, instance)
    current = instance.state if instance is not None else None
    nodes = [
        {
            "id": state,
            "label": state.replace("_", " ").replace("-", " "),
            "initial": state == spec.initial_state,
            "terminal": state in spec.terminal_states,
            "current": state == current,
        }
        for state in sorted(spec.states)
    ]
    edges = []
    for transition in spec.transitions:
        for source in sorted(transition.sources):
            edges.append(
                {
                    "id": f"{source}:{transition.event}:{transition.target}",
                    "from": source,
                    "to": transition.target,
                    "event": transition.event,
                    "label": transition.event.replace("_", " ").replace("-", " "),
                    "guarded": transition.guard is not None,
                    "reduces_data": transition.reducer is not None,
                    "metadata": copy.deepcopy(transition.metadata),
                }
            )
    return {
        "schema_version": 1,
        "machine": spec.name,
        "machine_version": spec.version,
        "instance_id": instance.instance_id if instance is not None else None,
        "current_state": current,
        "revision": instance.revision if instance is not None else None,
        "initial_state": spec.initial_state,
        "terminal_states": sorted(spec.terminal_states),
        "allowed_events": (
            list(spec.allowed_events(current)) if current is not None else []
        ),
        "nodes": nodes,
        "edges": edges,
        "metadata": copy.deepcopy(spec.metadata),
    }


__all__ = [
    "DispatchResult",
    "EventCollision",
    "FSMError",
    "Guard",
    "Invariant",
    "InvariantViolation",
    "MachineDefinitionError",
    "MachineInstance",
    "MachineSpec",
    "Reducer",
    "RevisionConflict",
    "TransitionContext",
    "TransitionRejected",
    "TransitionSpec",
    "acknowledge_events",
    "create_instance",
    "dispatch",
    "project_machine",
    "validate_instance",
]
