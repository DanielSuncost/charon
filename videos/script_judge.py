"""Script judge — critically evaluate narration tone against the 'earnest
toolmaker' rubric, via OpenRouter (text). Pairs with aesthetic_judge.py (vision).

    python videos/script_judge.py [feature_number]   # judge a script or the whole file

Returns JSON: {score 1-10, verdict, issues[], rewrites[{before,after}]}. The
rubric encodes the voice the user asked for: plain, clear, sincere, no marketing
and no *performed* anti-marketing. A harsh critic — 8+ means it would ship.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'videos' / 'scripts.md'

RUBRIC = """\
You are a ruthless script editor. The voice we want: a person who built a tool and
wants you to understand it. Plain, clear, sincere. NOT selling — and crucially NOT
making a show of *not* selling. Score the narration 1–10 and flag every line that
breaks the voice.

Reward:
- Plain declaratives that start from the thing itself ("Every conversation is saved
  on your machine").
- Concrete specifics and honest numbers, stated flatly, without comment on their honesty.
- Short sentences. Ordinary words.

Penalize hard (these make it sound like a smug nerd):
- Performed humility or self-aware honesty ("we measured it instead of asserting it",
  "so we don't pretend it does", "the honest hard case", "honesty reads as confidence").
- Rhetorical contrast / setups ("Most tools X. Charon Y.") used as a flex.
- Marketing adjectives ("powerful", "seamless", "revolutionary", "blazing").
- Any sentence a person slightly pleased with themselves would enjoy saying.
- Winking taglines at the end.

Output ONLY this JSON:
{"score": <1-10>, "verdict": "<one sentence>",
 "issues": ["<short problem + the offending phrase>", ...],
 "rewrites": [{"before": "<line>", "after": "<plainer line>"}, ...]}
"""


def _key() -> str:
    k = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if k:
        return k
    f = REPO / 'videos' / '.openrouter_key'
    return f.read_text(encoding='utf-8').strip() if f.exists() else ''


def _section(number: str | None) -> str:
    text = SCRIPTS.read_text(encoding='utf-8')
    if not number:
        return text
    # extract the "## NN — ..." block
    lines = text.splitlines()
    out, grab = [], False
    for ln in lines:
        if ln.startswith('## ') and f'{number} —' in ln:
            grab = True
        elif ln.startswith('## ') and grab:
            break
        if grab:
            out.append(ln)
    return '\n'.join(out) or text


def _judge_with_codex(script: str) -> dict:
    """Critique the script with headless `codex exec` (existing auth, no key)."""
    out = REPO / 'results' / 'videos' / '_script_judge_out.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(['codex', 'exec', '-o', str(out), f'{RUBRIC}\n\n--- SCRIPT ---\n{script}'],
                       cwd=str(REPO), check=True, capture_output=True, text=True, timeout=240)
        text = out.read_text(encoding='utf-8')
    except Exception as e:
        return {'score': None, 'verdict': f'codex script judge failed: {e}', 'error': 'codex_failed'}
    s, e = text.find('{'), text.rfind('}')
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return {'score': None, 'verdict': 'unparseable', 'raw': text[:400]}


def judge(number: str | None = None, model: str = 'anthropic/claude-sonnet-4') -> dict:
    script = _section(number)
    # Prefer headless codex (existing auth, no key) when available.
    if not os.environ.get('CHARON_SCRIPT_BACKEND') == 'openrouter' and shutil.which('codex'):
        return _judge_with_codex(script)
    key = _key()
    if not key:
        return {'score': None, 'verdict': 'No OpenRouter key and no codex CLI. '
                'Put a key in videos/.openrouter_key, or install codex.', 'error': 'no_backend'}
    try:
        r = httpx.post('https://openrouter.ai/api/v1/chat/completions',
                       headers={'authorization': f'Bearer {key}', 'content-type': 'application/json'},
                       json={'model': model, 'max_tokens': 1200, 'messages': [
                           {'role': 'user', 'content': f'{RUBRIC}\n\n--- SCRIPT ---\n{script}'}]},
                       timeout=120)
        r.raise_for_status()
        text = r.json()['choices'][0]['message']['content']
    except Exception as e:
        return {'score': None, 'verdict': f'judge call failed: {e}', 'error': 'call_failed'}
    s, e = text.find('{'), text.rfind('}')
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return {'score': None, 'verdict': 'unparseable', 'raw': text[:400]}


REWRITE_PROMPT = """\
You are rewriting feature-video narration to fix tone problems. Keep the EXACT
markdown structure — every `## NN — Title`, every `- **[on screen]** · *narration*`
bullet, the headers and notes. Change ONLY the italic narration text after each `·`.

Apply this voice: a person who built a tool and wants you to understand it. Plain,
clear, sincere. No selling, and no show of not-selling. Cut every rhetorical
contrast ("Most tools X, Charon Y"), every performed-honesty aside ("we test it so
we know"), every winking closer, every marketing adjective. Start each line from the
thing itself. Short sentences, ordinary words, a number when you have one.

Return the FULL rewritten markdown, nothing else.
"""


def rewrite(script_md: str, issues: list[str]) -> str:
    """Ask codex to rewrite the script markdown, fixing the issues."""
    out = REPO / 'results' / 'videos' / '_script_rewrite.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    issue_text = '\n'.join(f'- {i}' for i in issues)
    prompt = f'{REWRITE_PROMPT}\n\nKnown issues to fix:\n{issue_text}\n\n--- SCRIPT ---\n{script_md}'
    subprocess.run(['codex', 'exec', '-o', str(out), prompt],
                   cwd=str(REPO), check=True, capture_output=True, text=True, timeout=300)
    text = out.read_text(encoding='utf-8').strip()
    # strip any code fences codex might wrap around the markdown
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    return text.strip() + '\n'


def loop(target: float = 8.0, max_iter: int = 4) -> list[dict]:
    """Judge the whole scripts.md, rewrite, re-judge — until it passes."""
    history = []
    for it in range(max_iter):
        v = judge(None)
        score = v.get('score')
        history.append({'iter': it, 'score': score, 'verdict': v.get('verdict')})
        print(f'[iter {it}] score={score} — {v.get("verdict","")}')
        for i in v.get('issues', []):
            print('   -', i)
        if score is None:
            print('   judge unavailable; stopping.'); break
        if score >= target:
            print(f'PASS at {score} >= {target}'); break
        new_md = rewrite(SCRIPTS.read_text(encoding='utf-8'), v.get('issues', []))
        if len(new_md) > 200:
            SCRIPTS.write_text(new_md, encoding='utf-8')
            print('   rewrote scripts.md')
        else:
            print('   rewrite too short, keeping previous; stopping.'); break
    return history


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'loop':
        loop()
    else:
        num = sys.argv[1] if len(sys.argv) > 1 else None
        print(json.dumps(judge(num), indent=2, ensure_ascii=False))
