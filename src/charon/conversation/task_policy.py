"""Task execution guidance and bounded, observation-based loop detection."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

TASK_GUIDANCE = '''Task execution requirements:
- Inspect the task directory's available tests, fixtures, expected outputs and local
  setup before implementing or claiming done. Match exact paths and file forms:
  archive member roots, legacy config filenames, shebangs and output formats.
  Do not modify the oracle to make your answer pass. If it conflicts with the
  user's request, record the conflict; inaccessible/hidden tests are not evidence.
- Resolve the task's target runtime/service before changing it. Inspect launchers,
  shebangs, service configuration, interpreter paths and versions. Charon's own
  interpreter/venv and the first python/pip on PATH may belong to the harness.
  Use the target interpreter's explicit path and -m pip; verify in the same runtime
  or service the task exercises. Do not upgrade Charon's environment as a substitute.
- When a command, credential or service is missing, inspect local task setup,
  mocks, endpoints and installed alternatives before declaring a blocker. Prefer
  a working artifact/service in the provided environment over an unrun design.
- Before reporting success run the smallest task-specific oracle/comparison on the
  final artifact in that target runtime. Never say "verified" or "byte-identical"
  without actually running the check/comparison and inspecting its result. Report
  the command and result; distinguish implemented, checked, and unverified work.
- After repeated errors or unchanged observations, change strategy. Preserve the
  best partial artifact and record remaining failures before exhausting the budget.
'''

NON_INTERACTIVE_GUIDANCE = '''Non-interactive run: no user can answer. Clarify is
unavailable. Inspect local evidence, choose and explicitly record a reasonable
assumption in your response, then proceed. Do not stop merely to ask a question.
This does not authorize inventing credentials, bypassing permissions, or taking
an irreversible action whose required authorization is absent; report that blocker
and complete independent work instead.
'''


@dataclass
class LoopGuard:
    error_limit: int
    stagnant_limit: int
    errors: int = 0
    stagnant: int = 0
    warned: bool = False
    seen: set[str] = field(default_factory=set)

    def observe(self, name, result) -> tuple[str, str] | None:
        """Novel output is new evidence, not proof of a durable state change.

        Count repeated observations even when call arguments change. Tool-supplied
        state_changed=True explicitly signals progress. No unbounded tree scans.
        One warning allows a strategy change; recurrence ends this submission.
        """
        self.errors = self.errors + 1 if result.is_error else 0
        digest = hashlib.sha256((name + '\0' + result.content).encode()).hexdigest()
        changed = not result.is_error and (result.details or {}).get('state_changed') is True
        self.stagnant = self.stagnant + 1 if digest in self.seen and not changed else 0
        self.seen.add(digest)
        reason = ''
        if self.errors >= self.error_limit:
            reason = f'{self.errors} consecutive tool errors'
        elif self.stagnant >= self.stagnant_limit:
            reason = f'{self.stagnant} calls without new observations'
        if not reason:
            return None
        action = 'stop' if self.warned else 'warn'
        self.warned = True
        self.errors = self.stagnant = 0
        return action, reason
