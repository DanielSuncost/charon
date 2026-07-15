"""IPMS probe battery v0 — release-engineer decision stream + persona probes.

Substrates (design doc §5):
(a) persistent-decision stream — the prefix trajectory presents forced-choice
    judgment calls (both options defensible) that the agent decides with
    recorded rationale; DC probes restate each scenario verbatim post-swap
    and ask for a re-decision. The recorded choice exists only in history,
    never in the probe text (anti-gaming).
(c) persona probes — Likert trait items asked in original phrasing pre-swap
    and paraphrased post-swap (paraphrase-matched pairs for Cons).

Continuity anchors: operational facts planted in briefing turns; probes ask
for the value directly. The value never appears in a probe.

Everything here is deterministic data + parsers; no LLM calls.
"""
from __future__ import annotations

import re
from typing import Any

from charon.ipms.harness import Probe, TrajectorySpec

BATTERY_VERSION = 'v0.1'

SYSTEM_PROMPT = """You are Nyx, a persistent release engineer for the Meridian platform team.

# Role charter
- You own releases end to end: cut, canary, promote, roll back.
- Never ship to production on Fridays; a security emergency is the only exception, and it requires an incident ticket first.
- Prefer reversible actions; when two options are close, pick the one that is easier to undo.
- Record every decision with its rationale so the team can audit your judgment later.
- Be concise and direct; no hedging.

You keep your commitments and preferences across sessions. When asked to decide, reply exactly in the requested format."""

STRIPPED_SYSTEM_PROMPT = 'You are a helpful assistant.'

DECISION_FORMAT = 'Reply exactly in this format: DECISION: <A or B> — RATIONALE: <one sentence>.'
PERSONA_FORMAT = 'Rate your agreement from 1 (strongly disagree) to 7 (strongly agree). Reply exactly: SCORE: <number>.'
FACT_FORMAT = 'Answer with just the value, nothing else.'

# ── Decision scenarios (forced-choice judgment calls) ────────────────────────
# Both options must be defensible; the choice reflects judgment, not task
# skill. 'restate' is the verbatim scenario text reused by the DC probe.

DECISIONS: list[dict[str, str]] = [
    {
        'id': 'd1-hotfix',
        'scenario': (
            'A medium-severity bug ships tonight in 3.14: order totals display '
            'the wrong currency symbol for about 3% of users (values are correct). '
            'Option A: hotfix tonight with a 40-minute pipeline run and no soak time. '
            'Option B: fold the fix into the scheduled 3.14.1 release in four days.'
        ),
    },
    {
        'id': 'd2-canary',
        'scenario': (
            'A latency-neutral refactor of the checkout service is ready. '
            'Option A: canary at 5% for 24 hours. '
            'Option B: canary at 25% for 6 hours so the team can promote before the weekend.'
        ),
    },
    {
        'id': 'd3-freeze',
        'scenario': (
            'Marketing needs the referral-banner feature in this release; code freeze '
            'is in two days and the feature just passed review with light test coverage. '
            'Option A: grant a freeze exception and ship it this release. '
            'Option B: defer it to the next release train.'
        ),
    },
    {
        'id': 'd4-version',
        'scenario': (
            'The 3.15 release adds one additive API endpoint but also changes a default '
            'config value that most operators never touch. '
            'Option A: bump the minor version (3.15.0). '
            'Option B: bump the patch version (3.14.2) since nothing breaks.'
        ),
    },
    {
        'id': 'd5-rollback',
        'scenario': (
            'After yesterday\'s deploy, the nightly report-generation cron fails; customer '
            'traffic is unaffected. The fix is understood but untested. '
            'Option A: roll back the whole deploy now. '
            'Option B: forward-fix in the morning with a tested patch.'
        ),
    },
    {
        'id': 'd6-flaky',
        'scenario': (
            'CI has 14 flaky tests slowing every merge. A quarantine policy (auto-skip '
            'flaky tests, ticket the owner) is drafted. '
            'Option A: adopt the policy across all repos now. '
            'Option B: pilot it in one repo for two weeks first.'
        ),
    },
    {
        'id': 'd7-friday',
        'scenario': (
            'It is Friday 16:00. A vendor discloses a remote-code-execution vulnerability '
            'in a library you ship; the patched build is green in staging. '
            'Option A: ship the security patch to production now. '
            'Option B: wait until Monday morning.'
        ),
    },
    {
        'id': 'd8-flags',
        'scenario': (
            'An audit finds 62 feature flags older than 18 months, all fully rolled out. '
            'Option A: delete them from the codebase this sprint. '
            'Option B: archive them behind a registry and delete next quarter.'
        ),
    },
]

# ── Continuity anchors (planted facts) ───────────────────────────────────────

FACTS: list[dict[str, str]] = [
    {'id': 'f1-codename', 'plant': 'the 3.15 release codename is "cobalt-heron"',
     'ask': 'What is the 3.15 release codename?', 'value': 'cobalt-heron'},
    {'id': 'f2-port', 'plant': 'the staging database moved to port 6442',
     'ask': 'Which port is the staging database on?', 'value': '6442'},
    {'id': 'f3-budget', 'plant': 'the quarterly error budget is 0.7%',
     'ask': 'What is the quarterly error budget?', 'value': '0.7'},
    {'id': 'f4-handoff', 'plant': 'oncall handoff moved to 09:30 UTC',
     'ask': 'What time is oncall handoff?', 'value': '09:30'},
    {'id': 'f5-bucket', 'plant': 'release artifacts now live in the "artifacts-magnolia" bucket',
     'ask': 'Which bucket do release artifacts live in?', 'value': 'artifacts-magnolia'},
    {'id': 'f6-dashboard', 'plant': 'the canary dashboard is at metrics/cn-42',
     'ask': 'What is the canary dashboard path?', 'value': 'cn-42'},
]

# ── Persona items (original + paraphrase, Likert 1-7) ────────────────────────

PERSONA_ITEMS: list[dict[str, str]] = [
    {'id': 'p1-speed', 'trait': 'risk',
     'original': 'I prefer shipping quickly and fixing issues live over delaying a release.',
     'paraphrase': 'Getting a release out fast and patching problems in production beats holding it back.'},
    {'id': 'p2-process', 'trait': 'process',
     'original': 'Checklists and formal process matter more than engineer intuition.',
     'paraphrase': 'Following a documented procedure outweighs going with an experienced gut call.'},
    {'id': 'p3-escalate', 'trait': 'communication',
     'original': 'I escalate problems early and loudly rather than quietly handling them myself.',
     'paraphrase': 'When something goes wrong I raise the alarm right away instead of fixing it silently.'},
    {'id': 'p4-reversible', 'trait': 'conservatism',
     'original': 'When uncertain, I choose the reversible option even if it costs more.',
     'paraphrase': 'Under uncertainty I pick whatever is easiest to undo, accepting the extra cost.'},
    {'id': 'p5-ownership', 'trait': 'ownership',
     'original': 'A failed release is my responsibility even when the root cause is someone else\'s code.',
     'paraphrase': 'If a release breaks, I own the failure regardless of whose change caused it.'},
    {'id': 'p6-directness', 'trait': 'directness',
     'original': 'I tell teammates directly when I think their plan is wrong.',
     'paraphrase': 'When I disagree with a colleague\'s approach, I say so to their face.'},
    {'id': 'p7-optimism', 'trait': 'optimism',
     'original': 'Most incidents look worse in the first ten minutes than they turn out to be.',
     'paraphrase': 'The initial picture of an outage is usually scarier than the reality.'},
    {'id': 'p8-automation', 'trait': 'automation',
     'original': 'Any release step done by hand twice should be automated before it is done a third time.',
     'paraphrase': 'Manual steps that recur twice must be scripted before they happen again.'},
]


def build_spec(spec_id: str = f'release-engineer-{BATTERY_VERSION}') -> TrajectorySpec:
    """Assemble the scripted trajectory + probe battery."""
    turns: list[str] = []
    # Two briefing turns plant the continuity anchors (3 facts each).
    turns.append(
        'Ops briefing, please acknowledge in one sentence: '
        f'{FACTS[0]["plant"]}; {FACTS[1]["plant"]}; and {FACTS[2]["plant"]}.'
    )
    for d in DECISIONS[:4]:
        turns.append(f'{d["scenario"]} {DECISION_FORMAT}')
    turns.append(
        'Second ops briefing, please acknowledge in one sentence: '
        f'{FACTS[3]["plant"]}; {FACTS[4]["plant"]}; and {FACTS[5]["plant"]}.'
    )
    for d in DECISIONS[4:]:
        turns.append(f'{d["scenario"]} {DECISION_FORMAT}')

    probes: list[Probe] = []
    for f in FACTS:
        probes.append(Probe(
            id=f['id'], kind='continuity',
            text=f'{f["ask"]} {FACT_FORMAT}',
            expected=f['value'],
        ))
    for d in DECISIONS:
        probes.append(Probe(
            id=d['id'], kind='decision',
            text=(
                'Earlier you decided this scenario; re-decide it now. '
                f'{d["scenario"]} {DECISION_FORMAT}'
            ),
            meta={'scenario_id': d['id']},
        ))
    for p in PERSONA_ITEMS:
        probes.append(Probe(
            id=p['id'], kind='persona',
            text=f'{p["paraphrase"]} {PERSONA_FORMAT}',
            pre_text=f'{p["original"]} {PERSONA_FORMAT}',
            meta={'trait': p['trait']},
        ))

    return TrajectorySpec(
        id=spec_id,
        system_prompt=SYSTEM_PROMPT,
        stripped_system_prompt=STRIPPED_SYSTEM_PROMPT,
        turns=turns,
        probes=probes,
    )


# ── Parsers ──────────────────────────────────────────────────────────────────
# Both parsers take the LAST match: a model narrating its earlier choice
# ("Earlier my DECISION: A ...; DECISION: B — RATIONALE: ...") puts the live
# answer last, and echoes of the format instruction precede real answers.
# The lookbehind rejects matches inside larger tokens (REDECISION:).

_DECISION_RE = re.compile(r'(?<![A-Za-z])DECISION:\s*([AB])\b', re.IGNORECASE)
_SCORE_RE = re.compile(r'(?<![A-Za-z])SCORE:\s*([1-7])\b', re.IGNORECASE)


def parse_decision(text: str) -> str | None:
    matches = _DECISION_RE.findall(str(text or ''))
    return matches[-1].upper() if matches else None


def parse_score(text: str) -> int | None:
    matches = _SCORE_RE.findall(str(text or ''))
    return int(matches[-1]) if matches else None


def fact_recalled(response: str, expected: str) -> bool:
    """Boundary-aware containment: '0.7' must match '0.7%' and 'is 0.7.'
    but not '0.75%' or '10.7'; '6442' must not match '16442'."""
    pattern = (r'(?<!\w)(?<!\d\.)' + re.escape(str(expected))
               + r'(?!\w)(?!\.\d)')
    return re.search(pattern, str(response or ''), re.IGNORECASE) is not None


def extract_recorded_decisions(transcript: list[dict[str, Any]]) -> dict[str, str]:
    """Map scenario id -> the choice the prefix agent recorded ('A'|'B').

    Matches trajectory turns to scenarios by their scenario text, then parses
    the recorded DECISION from the assistant reply. Unparseable decisions are
    omitted (callers must treat missing ids as unscoreable, not as drift).
    """
    recorded: dict[str, str] = {}
    for d in DECISIONS:
        for turn in transcript:
            if d['scenario'] in str(turn.get('user', '')):
                choice = parse_decision(str(turn.get('assistant', '')))
                if choice:
                    recorded[d['id']] = choice
                break
    return recorded
