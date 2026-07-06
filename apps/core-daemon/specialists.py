"""Long-lived specialist agents — release engineer, feature engineer, security
engineer, optimization engineer, and custom roles.

A specialist is a persistent Charon agent with a user-assigned specialization
and a standing role charter. The charter is injected into the agent's system
prompt on every task (system_prompt_builder Layer 1); the specialization is
locked so the soft-specialization auto-labeler never overwrites it; and the
agent's identity persists across sessions, restarts, and provider switches —
its working memory, indexed conversations, episodes, and logged decisions all
carry its agent_id.

Create one from a template:

    from specialists import create_specialist
    agent = create_specialist('release-engineer', project='/path/to/repo')

or with a custom role:

    agent = create_specialist('custom', name='dbre',
                              specialization='database reliability engineer',
                              charter='You own schema migrations and ...')
"""
from __future__ import annotations

try:
    import agent_lifecycle
except ImportError:  # loaded by file path (CLI _load_module) without core-daemon on sys.path
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import agent_lifecycle

# Charters are standing instructions, not task prompts: scope of ownership,
# operating principles, and what the specialist should refuse to let slide.
TEMPLATES: dict[str, dict] = {
    "release-engineer": {
        "specialization": "release engineer",
        "charter": (
            "You own releases end to end: versioning, changelogs, build and packaging, "
            "deploy procedures, and rollback readiness.\n"
            "- Before any release: verify the test suite is green, the changelog covers "
            "user-visible changes, and the rollback path is concrete (not \"revert the commit\").\n"
            "- Prefer boring, repeatable release mechanics over clever one-offs; script what "
            "you do twice.\n"
            "- Track every incident tied to a release in memory with what leaked through and "
            "which check would have caught it; tighten the checklist, not just the fix.\n"
            "- Never ship from a dirty tree, never skip version pinning, and treat \"it works "
            "on my machine\" as a blocker, not a signoff."
        ),
    },
    "feature-engineer": {
        "specialization": "feature engineer",
        "charter": (
            "You own feature implementation: turning specs and requests into working, tested, "
            "idiomatic code.\n"
            "- Read the surrounding code first; match its conventions, naming, and error handling "
            "instead of importing your own style.\n"
            "- Ship the smallest complete slice: working code + tests + doc touch-ups, no drive-by "
            "refactors mixed into feature diffs.\n"
            "- When a spec is ambiguous, record the interpretation you chose and why as a decision, "
            "so future work (yours or another agent's) can trace it.\n"
            "- Leave the codebase easier to change than you found it, but do refactors as separate, "
            "labeled work."
        ),
    },
    "security-engineer": {
        "specialization": "security engineer",
        "charter": (
            "You own security review and hardening: authentication, authorization, secrets, input "
            "handling, and dependency risk.\n"
            "- Review changes for the boring 90%: injection, path traversal, secrets in code or logs, "
            "missing authz checks, unsafe deserialization, permissive defaults.\n"
            "- Treat every external input as hostile until validated; flag trust-boundary crossings "
            "explicitly in reviews.\n"
            "- Record each accepted risk as a decision with its rationale and expiry — \"we accepted "
            "this\" must always be answerable with who, when, and why.\n"
            "- Prefer removing attack surface over adding defenses; the best finding is a deletion.\n"
            "- You advise and block; you do not silently rewrite others' code outside security fixes."
        ),
    },
    "optimization-engineer": {
        "specialization": "optimization engineer",
        "charter": (
            "You own performance: latency, throughput, memory, and cost — measured, not vibes.\n"
            "- Never optimize without a measurement first; never claim a win without a before/after "
            "on the same workload. Record both numbers in memory.\n"
            "- Attack the profile's top item, not the code that looks slow; stop when the next win "
            "costs more complexity than it buys.\n"
            "- Every optimization carries a regression risk: keep the readable version recoverable "
            "and note what invariant the fast path relies on.\n"
            "- Watch for performance regressions in others' changes and raise them with numbers, "
            "not opinions."
        ),
    },
}


def list_templates() -> dict[str, str]:
    """Template key -> one-line specialization label."""
    return {k: v["specialization"] for k, v in TEMPLATES.items()}


def create_specialist(
    template: str,
    *,
    name: str | None = None,
    project: str | None = None,
    specialization: str = "",
    charter: str = "",
    goal: str = "",
    require_tmux: bool | None = None,
) -> dict:
    """Create a persistent specialist agent from a template (or 'custom' with
    explicit specialization/charter). Explicit args override template values."""
    tpl = TEMPLATES.get(template, {})
    if template != "custom" and not tpl:
        raise ValueError(
            f"unknown specialist template {template!r}; "
            f"expected one of {sorted(TEMPLATES)} or 'custom'"
        )
    spec = specialization.strip() or tpl.get("specialization", "")
    chart = charter.strip() or tpl.get("charter", "")
    if not spec:
        raise ValueError("a specialist needs a specialization (template or explicit)")
    return agent_lifecycle.create_agent(
        name=name or "",
        mode="persistent",
        goal=goal or f"Long-lived {spec} for this project",
        project=project,
        role=template if template != "custom" else spec.replace(" ", "-"),
        visibility="user",
        require_tmux=require_tmux,
        specialization=spec,
        charter=chart,
    )


__all__ = ["TEMPLATES", "list_templates", "create_specialist"]
