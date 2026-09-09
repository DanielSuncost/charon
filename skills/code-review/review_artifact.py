#!/usr/bin/env python3
"""Review artifact template + checker (stdlib only, run from anywhere).

    python3 skills/code-review/review_artifact.py template [> review.md]
    python3 skills/code-review/review_artifact.py check <path> [--json]
    python3 skills/code-review/review_artifact.py selftest

The format it enforces is specified in docs/review-artifact.md: five required
sections (findings, severity, evidence, test results, unresolved risks), every
finding tagged with a severity from the ladder and carrying evidence, and no
placeholder text where a claim is owed.

Exit code 0 = conforms, 1 = problems (printed one per line), 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SEVERITIES = ('blocker', 'major', 'minor', 'nit')

# Section key -> the substring that identifies its heading.
REQUIRED_SECTIONS = {
    'findings': 'finding',
    'severity': 'severit',
    'evidence': 'evidence',
    'test_results': 'test',
    'unresolved_risks': 'risk',
}

_PLACEHOLDERS = ('tbd', 'todo', 'n/a', 'fixme', 'xxx')
_FINDING_RE = re.compile(r'^-\s+\*\*(F\d+)\*\*\s*\[(\w+)\]\s*(.+)$')
_LOCATOR_RE = re.compile(r'[\w./-]+\.\w+:\d+')
_NO_FINDINGS_RE = re.compile(r'^no findings\b', re.I | re.M)

TEMPLATE = """# Review — <subject>

- **Reviewer:** <agent id or role>
- **Subject:** <branch, diff range, or paths reviewed>
- **Baseline:** <what it was compared against>

## Findings

- **F1** [major] <one-line claim: what is wrong, not what you feel about it>
- **F2** [nit] <one-line claim>

## Severity

| level | count | disposition |
|---|---|---|
| blocker | 0 | merge blocked |
| major | 1 | fix before merge |
| minor | 0 | fix now or file |
| nit | 1 | non-blocking |

Decision: <request changes | approve with follow-ups | approve>

## Evidence

**F1** — `path/to/file.py:42`

```
<the diff quote, command, or output that proves the claim>
```

**F2** — `path/to/other.py:7`

## Test results

```
$ ./.venv/bin/python -m pytest -q
<tail of the real output>

$ ./.venv/bin/ruff check .
<tail of the real output>
```

## Unresolved risks

- <what was not reviewed, what remains unknown, what could still break>
"""


def split_sections(text: str) -> dict[str, str]:
    """Map lowercased ``## heading`` -> body text."""
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith('## '):
            if current is not None:
                sections[current] = '\n'.join(buf).strip()
            current = line[3:].strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = '\n'.join(buf).strip()
    return sections


def _find_section(sections: dict[str, str], needle: str) -> tuple[str, str] | None:
    for heading, body in sections.items():
        if needle in heading:
            return heading, body
    return None


def _is_placeholder(body: str) -> bool:
    stripped = body.strip().strip('.').lower()
    if not stripped:
        return True
    return any(stripped == p or stripped.startswith(p + ' ') for p in _PLACEHOLDERS)


def check_text(text: str) -> list[str]:
    """Return a list of problems; empty means the artifact conforms."""
    problems: list[str] = []
    sections = split_sections(text)
    found: dict[str, str] = {}

    for key, needle in REQUIRED_SECTIONS.items():
        hit = _find_section(sections, needle)
        if hit is None:
            problems.append(f'missing required section: {key.replace("_", " ")}')
        else:
            found[key] = hit[1]

    findings_body = found.get('findings', '')
    findings: list[tuple[str, str, str]] = []
    if 'findings' in found:
        for line in findings_body.splitlines():
            match = _FINDING_RE.match(line.strip())
            if match:
                findings.append((match.group(1), match.group(2).lower(), match.group(3)))
            elif line.strip().startswith('- '):
                problems.append(f'finding not in "- **F1** [severity] claim" form: {line.strip()[:60]}')
        if not findings and not _NO_FINDINGS_RE.search(findings_body):
            problems.append('findings section lists nothing and does not say "No findings."')
        for fid, severity, claim in findings:
            if severity not in SEVERITIES:
                problems.append(f'{fid}: severity "{severity}" is not one of {", ".join(SEVERITIES)}')
            if len(claim.strip()) < 15:
                problems.append(f'{fid}: claim is too short to be actionable')

    if 'severity' in found:
        body = found['severity'].lower()
        if not any(level in body for level in SEVERITIES):
            problems.append('severity section names no level from the ladder')
        if 'decision:' not in body:
            problems.append('severity section has no "Decision:" line')

    if 'evidence' in found:
        body = found['evidence']
        for fid, _, _ in findings:
            if fid not in body:
                problems.append(f'{fid}: no evidence recorded')
        if findings and not (_LOCATOR_RE.search(body) or '```' in body):
            problems.append('evidence section cites no file:line locator and quotes no output')

    if 'test_results' in found:
        body = found['test_results']
        if _is_placeholder(body):
            problems.append('test results is empty or a placeholder')
        elif 'not run' in body.lower() and len(body.strip()) < 40:
            problems.append('test results says "not run" without saying why')

    if 'unresolved_risks' in found:
        body = found['unresolved_risks']
        if _is_placeholder(body):
            problems.append('unresolved risks is empty or a placeholder')
        elif re.fullmatch(r'[-*\s]*none\.?', body.strip(), re.I):
            problems.append('unresolved risks says "none" with no account of what was not covered')

    return problems


def _selftest() -> int:
    ok = check_text(_SAMPLE_GOOD)
    assert ok == [], f'template sample should pass, got: {ok}'
    assert check_text(TEMPLATE) == [], f'shipped template should pass, got: {check_text(TEMPLATE)}'

    cases = {
        'missing required section: test results': _SAMPLE_GOOD.replace('## Test results', '## Notes'),
        'F1: no evidence recorded': _SAMPLE_GOOD.replace('**F1** — `src/charon/tools/demo_tool.py:12`', '**F9** — none'),
        'unresolved risks says "none" with no account of what was not covered':
            _SAMPLE_GOOD.replace('- Rust TUI paths were not exercised.', 'None'),
        'test results is empty or a placeholder': re.sub(
            r'## Test results\n.*?\n## Unresolved', '## Test results\n\nTBD\n\n## Unresolved',
            _SAMPLE_GOOD, flags=re.S),
    }
    for expected, sample in cases.items():
        got = check_text(sample)
        assert expected in got, f'expected {expected!r} in {got!r}'

    bad_sev = _SAMPLE_GOOD.replace('[major]', '[urgent]')
    assert any('not one of' in p for p in check_text(bad_sev)), check_text(bad_sev)
    print('selftest: ok')
    return 0


_SAMPLE_GOOD = """# Review — demo

## Findings

- **F1** [major] Executor is registered in ALL_TOOL_DEFS but missing from TOOL_EXECUTORS.

## Severity

| level | count |
|---|---|
| major | 1 |

Decision: request changes

## Evidence

**F1** — `src/charon/tools/demo_tool.py:12`

```
$ grep -n "Demo" src/charon/tools/__init__.py
1465:    'Demo': None,
```

## Test results

```
$ ./.venv/bin/python -m pytest -q
1450 passed, 1 skipped in 85.37s
```

## Unresolved risks

- Rust TUI paths were not exercised.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('template', help='print the review artifact skeleton')
    check = sub.add_parser('check', help='check an artifact against the format')
    check.add_argument('path')
    check.add_argument('--json', action='store_true')
    sub.add_parser('selftest', help='run the built-in checks')

    args = parser.parse_args(argv)

    if args.command == 'template':
        print(TEMPLATE, end='')
        return 0
    if args.command == 'selftest':
        return _selftest()
    if args.command == 'check':
        path = Path(args.path)
        if not path.is_file():
            print(f'no such file: {path}', file=sys.stderr)
            return 2
        problems = check_text(path.read_text(encoding='utf-8', errors='ignore'))
        if args.json:
            print(json.dumps({'path': str(path), 'ok': not problems, 'problems': problems}, indent=2))
        elif problems:
            print(f'{path}: {len(problems)} problem(s)')
            for problem in problems:
                print(f'  - {problem}')
        else:
            print(f'{path}: ok')
        return 1 if problems else 0

    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
