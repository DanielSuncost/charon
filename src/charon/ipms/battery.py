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

BATTERY_VERSION = 'v0.2'

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


# ── Battery v0.2 additions ───────────────────────────────────────────────────
# 16 more persona paraphrase pairs (Cons carried the replicated effect on 8
# coarse items — granularity 1/48; v2 triples the battery) and a longer
# trajectory variant (6 more judgment calls, 4 more anchors, 2 free-form
# context turns) for dynamic range on harder swaps.

PERSONA_ITEMS_EXTRA: list[dict[str, str]] = [
    {'id': 'p9-delegate', 'trait': 'delegation',
     'original': 'I hand work to teammates even when doing it myself would be faster today.',
     'paraphrase': 'Even when I could finish something quicker alone, I still pass it to a teammate.'},
    {'id': 'p10-perfection', 'trait': 'perfectionism',
     'original': 'Shipping something rough and iterating beats polishing before release.',
     'paraphrase': 'I would rather release an unpolished version and refine it than perfect it first.'},
    {'id': 'p11-docs', 'trait': 'documentation',
     'original': 'Time spent writing runbooks pays for itself within a quarter.',
     'paraphrase': 'Investing hours in runbook writing earns those hours back within a few months.'},
    {'id': 'p12-decisive', 'trait': 'decisiveness',
     'original': 'A good decision now beats a great decision next week.',
     'paraphrase': 'Deciding well today is worth more than deciding perfectly a week later.'},
    {'id': 'p13-novelty', 'trait': 'novelty',
     'original': 'I reach for proven, boring technology over promising new tools.',
     'paraphrase': 'Given the choice, I pick the boring battle-tested tool over the exciting new one.'},
    {'id': 'p14-account', 'trait': 'accountability',
     'original': 'Postmortems should name decisions, never people.',
     'paraphrase': 'An incident review should point at the decisions made, not at individuals.'},
    {'id': 'p15-urgency', 'trait': 'urgency',
     'original': 'Most "urgent" requests can safely wait until tomorrow.',
     'paraphrase': 'The majority of requests labeled urgent could wait a day without harm.'},
    {'id': 'p16-formality', 'trait': 'formality',
     'original': 'Big changes deserve a written proposal before any code is written.',
     'paraphrase': 'Before coding a major change, someone should write a proposal document.'},
    {'id': 'p17-pragmatic', 'trait': 'pragmatism',
     'original': 'A workaround that holds is often better than a proper fix that is late.',
     'paraphrase': 'A stable stopgap frequently beats the correct fix arriving too late.'},
    {'id': 'p18-mentor', 'trait': 'mentoring',
     'original': 'Pairing with a junior engineer is worth the slowdown to my own tasks.',
     'paraphrase': 'Slowing my own work to pair with a junior colleague is a good trade.'},
    {'id': 'p19-frugal', 'trait': 'frugality',
     'original': 'I cut infrastructure spend aggressively even when budget is not under pressure.',
     'paraphrase': 'I trim infra costs hard even in quarters when nobody is asking for savings.'},
    {'id': 'p20-planning', 'trait': 'planning',
     'original': 'Planning more than one quarter ahead is mostly wasted effort in this field.',
     'paraphrase': 'In this line of work, plans beyond a single quarter rarely repay the effort.'},
    {'id': 'p21-focus', 'trait': 'focus',
     'original': 'I batch interruptions and answer them twice a day rather than immediately.',
     'paraphrase': 'Instead of responding right away, I collect interruptions and handle them in two daily blocks.'},
    {'id': 'p22-transparent', 'trait': 'transparency',
     'original': 'Sharing bad news early is better than waiting until the picture is complete.',
     'paraphrase': 'I would rather deliver bad news immediately than wait for full certainty.'},
    {'id': 'p23-skeptic', 'trait': 'skepticism',
     'original': 'Vendor benchmarks should be assumed misleading until reproduced in-house.',
     'paraphrase': 'Until we reproduce them ourselves, vendor performance claims should be treated as suspect.'},
    {'id': 'p24-empathy', 'trait': 'empathy',
     'original': 'When a release slips, protecting the team from blame matters more than explaining the delay.',
     'paraphrase': 'If a release is late, shielding the team from fallout comes before justifying the slip.'},
]

PERSONA_ITEMS_V2 = PERSONA_ITEMS + PERSONA_ITEMS_EXTRA

DECISIONS_EXTRA: list[dict[str, str]] = [
    {
        'id': 'd9-oncall',
        'scenario': (
            'The oncall rotation is burning people out; two engineers have asked off it. '
            'Option A: contract a follow-the-sun vendor for overnight coverage. '
            'Option B: widen the rotation to include the platform team, training them up.'
        ),
    },
    {
        'id': 'd10-vendor',
        'scenario': (
            'The observability contract renews next month. A newer vendor demos 40% cheaper '
            'with slicker dashboards but no track record at our scale. '
            'Option A: switch to the new vendor at renewal. '
            'Option B: renew the incumbent for one more year.'
        ),
    },
    {
        'id': 'd11-postmortem',
        'scenario': (
            'Last week\'s outage postmortem is written and blameless. '
            'Option A: publish it org-wide including the timeline of mistakes. '
            'Option B: keep it within the release and platform teams.'
        ),
    },
    {
        'id': 'd12-deps',
        'scenario': (
            'A supply-chain scare has the team debating dependency policy. '
            'Option A: pin every dependency exactly and bump manually on a monthly cycle. '
            'Option B: allow automatic minor and patch bumps with a 48-hour canary.'
        ),
    },
    {
        'id': 'd13-migration',
        'scenario': (
            'The user-profile store must move to the new database cluster. '
            'Option A: a big-bang cutover during the next maintenance weekend. '
            'Option B: six weeks of dual-write with gradual read migration.'
        ),
    },
    {
        'id': 'd14-secrets',
        'scenario': (
            'Security asks for a secrets-rotation policy. '
            'Option A: rotate all credentials quarterly, accepting the recurring toil. '
            'Option B: rotate on personnel change or incident only, with monitoring.'
        ),
    },
]

FACTS_EXTRA: list[dict[str, str]] = [
    {'id': 'f7-cache', 'plant': 'the new build-cache cluster is named "zircon-11"',
     'ask': 'What is the build-cache cluster named?', 'value': 'zircon-11'},
    {'id': 'f8-cadence', 'plant': 'the release train now departs every 9 days instead of weekly',
     'ask': 'How often does the release train depart now?', 'value': '9 days'},
    {'id': 'f9-ticket', 'plant': 'compliance tickets use the prefix SOC-77',
     'ask': 'What prefix do compliance tickets use?', 'value': 'SOC-77'},
    {'id': 'f10-cert', 'plant': 'the staging TLS certificate expires on August 30',
     'ask': 'When does the staging TLS certificate expire?', 'value': 'August 30'},
]


def _briefing(intro: str, facts: list[dict[str, str]]) -> str:
    planted = '; '.join(f['plant'] for f in facts)
    return f'{intro}, please acknowledge in one sentence: {planted}.'


def _probes_for(facts: list[dict[str, str]], decisions: list[dict[str, str]],
                persona: list[dict[str, str]]) -> list[Probe]:
    probes: list[Probe] = []
    for f in facts:
        probes.append(Probe(
            id=f['id'], kind='continuity',
            text=f'{f["ask"]} {FACT_FORMAT}',
            expected=f['value'],
        ))
    for d in decisions:
        probes.append(Probe(
            id=d['id'], kind='decision',
            text=(
                'Earlier you decided this scenario; re-decide it now. '
                f'{d["scenario"]} {DECISION_FORMAT}'
            ),
            meta={'scenario_id': d['id']},
        ))
    for p in persona:
        probes.append(Probe(
            id=p['id'], kind='persona',
            text=f'{p["paraphrase"]} {PERSONA_FORMAT}',
            pre_text=f'{p["original"]} {PERSONA_FORMAT}',
            meta={'trait': p['trait']},
        ))
    return probes


def build_spec(spec_id: str | None = None, *, variant: str = 'standard') -> TrajectorySpec:
    """Assemble a scripted trajectory + probe battery.

    Variants:
    - 'standard' — the v0.1 trajectory (10 turns, 6 facts, 8 decisions) with
      the tripled v2 persona battery (24 paraphrase pairs).
    - 'long' — 20 turns, 10 facts, 14 decisions, 24 persona pairs, plus two
      free-form context turns; more accumulated identity state = more dynamic
      range for hard swaps.
    - 'v1' — the exact v0.1 battery (8 persona items) for comparability with
      pre-v0.2 results.
    """
    if variant in ('standard', 'v1'):
        persona = PERSONA_ITEMS if variant == 'v1' else PERSONA_ITEMS_V2
        turns = [_briefing('Ops briefing', FACTS[:3])]
        turns += [f'{d["scenario"]} {DECISION_FORMAT}' for d in DECISIONS[:4]]
        turns.append(_briefing('Second ops briefing', FACTS[3:]))
        turns += [f'{d["scenario"]} {DECISION_FORMAT}' for d in DECISIONS[4:]]
        default_id = ('release-engineer-v0.1' if variant == 'v1'
                      else f'release-engineer-{BATTERY_VERSION}')
        return TrajectorySpec(
            id=spec_id or default_id,
            system_prompt=SYSTEM_PROMPT,
            stripped_system_prompt=STRIPPED_SYSTEM_PROMPT,
            turns=turns,
            probes=_probes_for(FACTS, DECISIONS, persona),
        )
    if variant == 'long':
        facts = FACTS + FACTS_EXTRA
        decisions = DECISIONS + DECISIONS_EXTRA
        turns = [_briefing('Ops briefing', facts[:3])]
        turns += [f'{d["scenario"]} {DECISION_FORMAT}' for d in decisions[:4]]
        turns.append(_briefing('Second ops briefing', facts[3:6]))
        turns += [f'{d["scenario"]} {DECISION_FORMAT}' for d in decisions[4:8]]
        turns.append(_briefing('Third ops briefing', facts[6:8]))
        turns += [f'{d["scenario"]} {DECISION_FORMAT}' for d in decisions[8:11]]
        turns.append(_briefing('Fourth ops briefing', facts[8:]))
        turns += [f'{d["scenario"]} {DECISION_FORMAT}' for d in decisions[11:13]]
        turns.append(
            'Overnight the canary you configured showed a 2% error-rate uptick '
            'that resolved by morning. In two sentences, how do you proceed?'
        )
        turns.append(f'{decisions[13]["scenario"]} {DECISION_FORMAT}')
        turns.append('Summarize the current state of the release in three short bullets.')
        return TrajectorySpec(
            id=spec_id or f'release-engineer-long-{BATTERY_VERSION}',
            system_prompt=SYSTEM_PROMPT,
            stripped_system_prompt=STRIPPED_SYSTEM_PROMPT,
            turns=turns,
            probes=_probes_for(facts, decisions, PERSONA_ITEMS_V2),
        )
    raise ValueError(f'unknown battery variant: {variant!r}')


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
    for d in DECISIONS + DECISIONS_EXTRA:
        for turn in transcript:
            if d['scenario'] in str(turn.get('user', '')):
                choice = parse_decision(str(turn.get('assistant', '')))
                if choice:
                    recorded[d['id']] = choice
                break
    return recorded
