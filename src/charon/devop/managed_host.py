"""Process host shared by Charon's long-running managed runners.

A managed runner is its own process (launched through the run-process tool
with a new session), so it outlives the chat thread that started it. This
module holds what every such runner needs and nothing about what it runs:

* SIGTERM is the stop signal. The handler runs ``on_stop`` and then raises
  SystemExit(143) in the main thread, which interrupts the body wherever it
  is, including inside a tool call or an action sequence. A stop is never
  deferred to the next loop iteration.
* ``stop_requested`` is polled once a second on a daemon thread; when it
  turns true the process sends itself SIGTERM, so a flag written by another
  process (a chat command, a kill switch) lands as the same interruption.
* An exception from the body is reported through ``on_failure`` and
  re-raised, so the runner exits non-zero with its state recorded.

Nothing here caps how long the body may run. Budgets belong to the work.
"""
from __future__ import annotations

import os
import signal
import threading
from typing import Any, Callable


def run_managed(
    body: Callable[[], Any],
    *,
    stop_requested: Callable[[], bool],
    on_stop: Callable[[], None],
    on_failure: Callable[[BaseException], None],
    poll_seconds: float = 1.0,
) -> Any:
    def stop(*_):
        on_stop()
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop)
    done = threading.Event()

    def watch_stop():
        while not done.wait(poll_seconds):
            try:
                wanted = bool(stop_requested())
            except Exception:
                wanted = False
            if wanted:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=watch_stop, name='managed-stop-watch', daemon=True).start()
    try:
        return body()
    except Exception as exc:
        on_failure(exc)
        raise
    finally:
        done.set()
