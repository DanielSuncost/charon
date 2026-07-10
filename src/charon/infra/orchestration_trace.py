"""Unified orchestration trace/span substrate — the shared observability +
correlation-ID backbone for every Charon orchestrator (Libris, devop, batch,
judge, shades, agent runtime).

Today each system rolls its own JSONL with no shared IDs, so nothing links a
shade's tokens to its phase to its parent task to the operation. This module is
the one place all of them emit spans, tagged with correlation IDs
(trace_id / span_id / parent_span_id + operation_id / agent_id / task_id), so a
whole multi-agent run can be reconstructed as one tree and one timeline.

It is deliberately additive and non-breaking: existing event logs keep working;
spans are a new, parallel stream. Storage is a single append-only JSONL
(`traces/spans.jsonl`) under the state dir — a span is written once at start
(status=running) and once at end (status=ok/error/...); the reader coalesces by
span_id (last write wins) so in-flight runs are visible and completed runs carry
timing/tokens/cost.

Design notes:
- stdlib only (matches the codebase; no external tracing deps).
- I/O (prompts/responses/tool args) is NOT stored inline — pass `io_ref` pointing
  at a blob the caller already persisted, to keep the span stream small.
- cost is estimated centrally (`estimate_cost_usd`) so cost exists everywhere,
  not just Libris.

Typical use:

    from charon.infra.orchestration_trace import span, new_trace_id
    tid = new_trace_id()
    with span(state_dir, name="coordinator scout", system="libris", kind="agent_run",
              trace_id=tid, operation_id=op_id, agent_id=ag_id) as sp:
        ... do work ...
        sp.add_usage(model="gpt-5.5", input_tokens=1200, output_tokens=300)

or for an already-complete unit:

    record_span(state_dir, name="tool: Web", system="libris", kind="tool_call",
                trace_id=tid, parent_span_id=sp.span_id, duration_ms=812,
                status="ok")
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SPAN_KINDS = (
    "operation",   # a whole orchestrated run (libris op, devop op, batch, judge loop)
    "step",        # one controller step / super-step
    "agent_run",   # one agent's turn/execution
    "llm_call",    # a single model call
    "tool_call",   # a single tool invocation
    "phase",       # a shade/contract phase
    "fanout",      # a spawn-N / barrier group
)

SPAN_STATUS = ("running", "ok", "error", "interrupted", "skipped")


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


def new_span_id() -> str:
    return f"sp_{uuid.uuid4().hex[:16]}"


def _spans_path(state_dir: Path) -> Path:
    d = Path(state_dir) / "traces"
    d.mkdir(parents=True, exist_ok=True)
    return d / "spans.jsonl"


def _append(state_dir: Path, row: dict) -> None:
    try:
        path = _spans_path(state_dir)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # tracing must never break the traced work


# ── cost estimation (generalized from Libris; covers tier keys + real models) ──

# rates are USD per 1M tokens (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    # tier aliases used by the model policy
    "cheap": (0.0, 0.0), "cheap_local": (0.0, 0.0), "local": (0.0, 0.0),
    "fast": (0.15, 0.60), "strong": (3.00, 15.00),
    # known model families (prefix match)
    "gpt-5.5": (1.25, 10.00), "gpt-5": (1.25, 10.00), "gpt-4o": (2.50, 10.00),
    "o3": (2.00, 8.00), "o4": (2.00, 8.00),
    "claude-opus": (15.00, 75.00), "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00), "claude-fable": (3.00, 15.00),
    "gemini": (1.25, 5.00), "llama": (0.0, 0.0), "qwen": (0.0, 0.0),
    "bge": (0.0, 0.0),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Central cost estimate. Matches an exact tier key, else the longest model-id
    prefix, else falls back to the 'fast' tier. Returns USD."""
    key = (model or "").strip().lower()
    rates = _PRICING.get(key)
    if rates is None:
        # longest prefix match against known families
        best = ""
        for pfx in _PRICING:
            if key.startswith(pfx) and len(pfx) > len(best):
                best = pfx
        rates = _PRICING.get(best, _PRICING["fast"])
    in_rate, out_rate = rates
    return round((input_tokens / 1_000_000.0) * in_rate
                 + (output_tokens / 1_000_000.0) * out_rate, 6)


# ── span model ────────────────────────────────────────────────────────────

@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    system: str
    kind: str
    parent_span_id: str | None = None
    operation_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    status: str = "running"
    start_ts: float = 0.0
    end_ts: float | None = None
    duration_ms: float | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    io_ref: dict | None = None
    attributes: dict = field(default_factory=dict)
    created_at: str = ""

    def add_usage(self, *, model: str | None = None, input_tokens: int = 0,
                  output_tokens: int = 0, cost_usd: float | None = None) -> None:
        """Accumulate token usage onto the span; cost auto-estimated if not given."""
        if model:
            self.model = model
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.total_tokens = self.input_tokens + self.output_tokens
        if cost_usd is not None:
            self.cost_usd += float(cost_usd)
        elif input_tokens or output_tokens:
            self.cost_usd += estimate_cost_usd(self.model or "", input_tokens, output_tokens)
        self.cost_usd = round(self.cost_usd, 6)

    def set(self, **attrs) -> None:
        self.attributes.update(attrs)

    def _row(self) -> dict:
        return asdict(self)


def _make_span(**kw) -> Span:
    kind = kw.get("kind", "step")
    if kind not in SPAN_KINDS:
        kw["kind"] = "step"
    return Span(
        span_id=kw.get("span_id") or new_span_id(),
        trace_id=kw.get("trace_id") or new_trace_id(),
        name=str(kw.get("name") or kw.get("kind") or "span"),
        system=str(kw.get("system") or "unknown"),
        kind=kw.get("kind", "step"),
        parent_span_id=kw.get("parent_span_id"),
        operation_id=kw.get("operation_id"),
        agent_id=kw.get("agent_id"),
        task_id=kw.get("task_id"),
        model=kw.get("model"),
        io_ref=kw.get("io_ref"),
        attributes=dict(kw.get("attributes") or {}),
        start_ts=_now_ts(),
        created_at=_now_iso(),
    )


@contextmanager
def span(state_dir: Path, **kw) -> Iterator[Span]:
    """Context manager: writes a running span on enter and a terminal span on exit
    (status ok, or error with the exception message — the exception is re-raised).
    Duration is computed from wall-clock. `state_dir` may be None to no-op-persist
    (still yields a usable Span for tests)."""
    sp = _make_span(**kw)
    if state_dir is not None:
        _append(state_dir, sp._row())
    try:
        yield sp
    except BaseException as e:  # noqa: BLE001 - record then re-raise
        sp.status = "error"
        sp.error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        sp.end_ts = _now_ts()
        sp.duration_ms = round((sp.end_ts - sp.start_ts) * 1000.0, 1)
        if sp.status == "running":
            sp.status = "ok"
        if state_dir is not None:
            _append(state_dir, sp._row())


def record_span(state_dir: Path, **kw) -> Span:
    """Emit a single already-complete span in one shot (no separate start row).
    Computes duration from start_ts/end_ts or an explicit duration_ms."""
    sp = _make_span(**kw)
    sp.status = kw.get("status") or "ok"
    if "input_tokens" in kw or "output_tokens" in kw:
        sp.add_usage(model=kw.get("model"),
                     input_tokens=int(kw.get("input_tokens") or 0),
                     output_tokens=int(kw.get("output_tokens") or 0),
                     cost_usd=kw.get("cost_usd"))
    if kw.get("duration_ms") is not None:
        sp.duration_ms = float(kw["duration_ms"])
        sp.end_ts = sp.start_ts + sp.duration_ms / 1000.0
    else:
        sp.end_ts = _now_ts()
        sp.duration_ms = round((sp.end_ts - sp.start_ts) * 1000.0, 1)
    if kw.get("error"):
        sp.status = "error"
        sp.error = str(kw["error"])[:500]
    if state_dir is not None:
        _append(state_dir, sp._row())
    return sp


# ── reading / querying ────────────────────────────────────────────────────

def _coalesce(rows: list[dict]) -> list[dict]:
    """Merge start/end rows sharing a span_id — last write wins, but never let a
    later row blank out fields an earlier one set (running row has no duration)."""
    by_id: dict[str, dict] = {}
    for r in rows:
        sid = r.get("span_id")
        if not sid:
            continue
        if sid in by_id:
            merged = dict(by_id[sid])
            for k, v in r.items():
                if v not in (None, 0, 0.0, "", {}) or k not in merged:
                    merged[k] = v
            # terminal status must win over "running"
            if r.get("status") and r.get("status") != "running":
                merged["status"] = r["status"]
            by_id[sid] = merged
        else:
            by_id[sid] = dict(r)
    return list(by_id.values())


def read_spans(state_dir: Path, *, trace_id: str | None = None,
               operation_id: str | None = None, agent_id: str | None = None,
               system: str | None = None, since_ts: float | None = None,
               limit: int | None = None) -> list[dict]:
    """Coalesced spans (one dict per span_id), filtered and sorted by start_ts."""
    path = _spans_path(state_dir)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    spans = _coalesce(rows)
    def keep(s: dict) -> bool:
        if trace_id and s.get("trace_id") != trace_id:
            return False
        if operation_id and s.get("operation_id") != operation_id:
            return False
        if agent_id and s.get("agent_id") != agent_id:
            return False
        if system and s.get("system") != system:
            return False
        if since_ts and (s.get("start_ts") or 0) < since_ts:
            return False
        return True
    spans = [s for s in spans if keep(s)]
    spans.sort(key=lambda s: s.get("start_ts") or 0.0)
    if limit:
        spans = spans[-limit:]
    return spans


def build_span_tree(spans: list[dict]) -> list[dict]:
    """Nest coalesced spans by parent_span_id into a forest. Each node gets a
    'children' list; orphans (missing parent) surface as roots."""
    nodes = {s["span_id"]: {**s, "children": []} for s in spans if s.get("span_id")}
    roots = []
    for s in nodes.values():
        parent = s.get("parent_span_id")
        if parent and parent in nodes:
            nodes[parent]["children"].append(s)
        else:
            roots.append(s)
    for n in nodes.values():
        n["children"].sort(key=lambda c: c.get("start_ts") or 0.0)
    roots.sort(key=lambda c: c.get("start_ts") or 0.0)
    return roots


def trace_summary(state_dir: Path, trace_id: str) -> dict:
    """Roll-up for one trace: span counts, total tokens, total cost, wall-clock,
    error count — the numbers a run header/dashboard wants."""
    spans = read_spans(state_dir, trace_id=trace_id)
    if not spans:
        return {"trace_id": trace_id, "spans": 0}
    starts = [s.get("start_ts") for s in spans if s.get("start_ts")]
    ends = [s.get("end_ts") for s in spans if s.get("end_ts")]
    return {
        "trace_id": trace_id,
        "spans": len(spans),
        "by_system": _count(spans, "system"),
        "by_kind": _count(spans, "kind"),
        "input_tokens": sum(int(s.get("input_tokens") or 0) for s in spans),
        "output_tokens": sum(int(s.get("output_tokens") or 0) for s in spans),
        "total_tokens": sum(int(s.get("total_tokens") or 0) for s in spans),
        "cost_usd": round(sum(float(s.get("cost_usd") or 0.0) for s in spans), 6),
        "errors": sum(1 for s in spans if s.get("status") == "error"),
        "wall_ms": round((max(ends) - min(starts)) * 1000.0, 1) if starts and ends else None,
        "started_at": min(starts) if starts else None,
    }


def timeline(state_dir: Path, *, limit: int = 200,
             since_ts: float | None = None) -> list[dict]:
    """Cross-system chronological span list — the unified activity feed that spans
    ALL orchestrators (the thing LangGraph doesn't have, because its world is one
    graph). Compact rows for display."""
    spans = read_spans(state_dir, since_ts=since_ts)
    spans.sort(key=lambda s: s.get("start_ts") or 0.0)
    if limit:
        spans = spans[-limit:]
    out = []
    for s in spans:
        out.append({
            "start_ts": s.get("start_ts"),
            "system": s.get("system"),
            "kind": s.get("kind"),
            "name": s.get("name"),
            "status": s.get("status"),
            "duration_ms": s.get("duration_ms"),
            "operation_id": s.get("operation_id"),
            "agent_id": s.get("agent_id"),
            "cost_usd": s.get("cost_usd"),
            "trace_id": s.get("trace_id"),
            "span_id": s.get("span_id"),
        })
    return out


def _count(spans: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for s in spans:
        k = str(s.get(key) or "")
        out[k] = out.get(k, 0) + 1
    return out


__all__ = [
    "Span", "span", "record_span", "read_spans", "build_span_tree",
    "trace_summary", "timeline", "estimate_cost_usd", "new_trace_id",
    "new_span_id", "SPAN_KINDS", "SPAN_STATUS",
]
