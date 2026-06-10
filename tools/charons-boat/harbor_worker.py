#!/usr/bin/env python3
"""harbor_worker.py — Remote-side Harbor protocol handler.

Runs on the remote machine via `charons-boat harbor`.
Reads harbor.dispatch messages from stdin, executes tasks,
relays harbor.recall queries back to the Harbor (local Charon),
and returns structured results.

Wire protocol: JSON lines over stdio (same transport as boat_stream.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HARBOR_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send(msg: dict) -> None:
    """Send a JSON message to Harbor (stdout)."""
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read_line() -> dict | None:
    """Read one JSON message from Harbor (stdin). Returns None on EOF."""
    try:
        line = sys.stdin.readline()
        if not line:
            return None
        return json.loads(line.strip())
    except (json.JSONDecodeError, EOFError):
        return None


def _worker_id() -> str:
    """Generate a worker identifier from hostname and PID."""
    import socket
    return f"{socket.gethostname()}:{os.getpid()}"


# ── Recall relay ─────────────────────────────────────────────────────

class RecallRelay:
    """Handles mid-task memory recall by querying Harbor over stdio.

    The worker writes harbor.recall to stdout, Harbor responds with
    harbor.recall_result on stdin. This class manages the request/response
    matching with a simple counter + threading event.
    """

    def __init__(self):
        self._counter = 0
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}

    def recall(self, query: str, voyage_id: str, limit: int = 10,
               container_tag: str | None = None, timeout: float = 30.0) -> list[dict]:
        """Send a recall query to Harbor and wait for the response."""
        with self._lock:
            self._counter += 1
            request_id = f"rq-{self._counter:04d}"
            event = threading.Event()
            self._pending[request_id] = event

        msg = {
            "type": "harbor.recall",
            "request_id": request_id,
            "voyage_id": voyage_id,
            "query": query,
            "limit": limit,
        }
        if container_tag:
            msg["container_tag"] = container_tag
        _send(msg)

        if event.wait(timeout):
            with self._lock:
                return self._results.pop(request_id, {}).get("memories", [])
        return []

    def deliver(self, request_id: str, result: dict) -> None:
        """Called when a harbor.recall_result arrives from Harbor."""
        with self._lock:
            self._results[request_id] = result
            event = self._pending.pop(request_id, None)
        if event:
            event.set()


# ── Task execution ───────────────────────────────────────────────────

def _execute_bare(manifest: dict, recall: RecallRelay) -> dict:
    """Execute a bare command/script with the manifest as context.

    The script receives the manifest path via HARBOR_MANIFEST env var
    and the instruction via stdin.
    """
    voyage_id = manifest.get("voyage_id", "unknown")
    instruction = manifest.get("instruction", "")
    timeout = manifest.get("timeout_seconds", 1800)
    project_root = manifest.get("project_root", os.getcwd())

    # Write manifest to temp file for the script to read
    manifest_dir = Path(tempfile.gettempdir()) / "harbor"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / f"{voyage_id}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    _send({
        "type": "harbor.progress",
        "voyage_id": voyage_id,
        "status": "running",
        "step": "1/1",
        "summary": f"Executing: {instruction[:80]}",
    })

    env = {**os.environ, "HARBOR_MANIFEST": str(manifest_path), "HARBOR_VOYAGE_ID": voyage_id}

    try:
        result = subprocess.run(
            ["bash", "-c", instruction],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_root if Path(project_root).is_dir() else None,
            env=env,
        )

        return {
            "stdout": result.stdout[-10000:] if len(result.stdout) > 10000 else result.stdout,
            "stderr": result.stderr[-5000:] if len(result.stderr) > 5000 else result.stderr,
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s", "returncode": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}
    finally:
        # Clean up manifest
        try:
            manifest_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Main loop ────────────────────────────────────────────────────────

def _handle_dispatch(msg: dict, recall: RecallRelay) -> None:
    """Handle a harbor.dispatch message."""
    manifest = msg.get("manifest", {})
    voyage_id = manifest.get("voyage_id", msg.get("voyage_id", "unknown"))
    manifest["voyage_id"] = voyage_id

    worker_id = _worker_id()

    # Acknowledge
    _send({
        "type": "harbor.ack",
        "voyage_id": voyage_id,
        "worker_id": worker_id,
        "status": "started",
    })

    # Execute
    agent_type = manifest.get("target_agent_type", "bare")

    if agent_type in ("bare", "bash", "script"):
        result = _execute_bare(manifest, recall)
    else:
        # Future: interactive agent wrapping (Phase 2)
        result = _execute_bare(manifest, recall)

    # Send result
    _send({
        "type": "harbor.result",
        "voyage_id": voyage_id,
        "status": "completed" if result.get("success") else "failed",
        "result": result,
        "completed_at": _now(),
    })


def main():
    """Main harbor worker loop. Reads JSON lines from stdin."""
    recall = RecallRelay()
    worker_id = _worker_id()

    # Signal readiness
    _send({
        "type": "harbor.ready",
        "worker_id": worker_id,
        "version": HARBOR_VERSION,
        "ts": _now(),
    })

    while True:
        msg = _read_line()
        if msg is None:
            break

        msg_type = msg.get("type", "")

        if msg_type == "harbor.dispatch":
            # Run in a thread so we can still receive recall_result on stdin
            thread = threading.Thread(
                target=_handle_dispatch,
                args=(msg, recall),
                daemon=True,
            )
            thread.start()

        elif msg_type == "harbor.recall_result":
            # Deliver recall response to waiting thread
            request_id = msg.get("request_id", "")
            recall.deliver(request_id, msg)

        elif msg_type == "harbor.abort":
            # Future: signal running task to stop
            _send({
                "type": "harbor.ack",
                "voyage_id": msg.get("voyage_id", ""),
                "worker_id": worker_id,
                "status": "abort_received",
            })

        elif msg_type == "harbor.ping":
            _send({"type": "harbor.pong", "ts": _now()})


if __name__ == "__main__":
    main()
