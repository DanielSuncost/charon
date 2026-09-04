# ADR-0003: Lifecycle State Machines and Workflow Graphs

Status: accepted
Date: 2026-07-20

## Context

Charon coordinates work at two different levels:

- a workflow has topology: branches, fan-out, joins, retries, waits, and loops;
- a durable entity has a lifecycle: only certain events are legal from each
  state, terminal states are absorbing, and completion has domain-specific
  invariants.

Representing both concerns with ad-hoc status strings permits impossible
states. Representing both concerns with one large state machine makes parallel
work and graph inspection unnecessarily difficult.

## Decision

Use directed workflow graphs for topology and small finite state machines for
durable lifecycles.

```text
command
  -> guarded lifecycle transition
  -> atomic snapshot + event outbox
  -> workflow scheduling / side effects
  -> read-model projections
  -> TUI, tools, reports, and APIs
```

The shared state-machine kernel provides:

- explicit states, events, and legal transitions;
- guards, reducers, and invariants;
- monotonic revisions and optimistic concurrency checks;
- caller-supplied event IDs for idempotent replay;
- collision detection when an event ID is reused with different content;
- terminal-state absorption;
- transition records staged in the same snapshot as state through an outbox.

Persistence adapters must serialize writers, atomically replace snapshots, and
publish committed outbox rows idempotently. An event log is an audit projection;
the snapshot remains the restart cursor.

## Authority and projections

Lifecycle state is authoritative. Existing operation, topic, dashboard, and
agent-communication documents are compatibility projections. Projection repair
must never invent a transition, change the lifecycle timestamp, or make an old
operation appear newer.

Workflow-run status and domain status remain distinct. For example, a scheduler
can finish executing its nodes while a domain completion guard still rejects
delivery. User-facing completion is emitted only after the domain transition
commits.

## Completion protocol

Completion is a guarded transition, not an arbitrary status assignment.

1. Produce the candidate artifacts.
2. Verify domain invariants.
3. Attest the selected artifacts with stable content digests.
4. Persist a completion certificate.
5. Dispatch the completion event.
6. Project the committed terminal state to user-facing views.

Artifact integrity after completion is reported separately from historical
lifecycle completion. Damaged or missing artifacts do not rewrite history, but
they do make the current delivery projection unavailable until repaired.

## Adoption

Libris is the first migrated domain because its operation, topic, and delivery
states cross multiple workers and user interfaces. Other coordination systems
should adopt the kernel when they have durable lifecycle invariants; they do not
need an FSM merely because they contain a short local control flow.

Migration is incremental:

- preserve existing external read contracts;
- adopt valid historical state once and record reconciliation metadata;
- quarantine ambiguous ownership rather than guessing;
- move one lifecycle boundary at a time;
- keep workflow graphs independently inspectable.

## Consequences

Positive:

- illegal transitions fail before corrupting durable state;
- retries and restarts are idempotent and observable;
- completion means a verifiable domain outcome;
- graph visualization remains focused on coordination topology;
- interfaces can evolve without becoming persistence authorities.

Costs:

- each durable domain needs an explicit transition table and invariants;
- compatibility projections and migration paths require tests;
- side effects must carry idempotency keys and obey the snapshot/outbox boundary.
