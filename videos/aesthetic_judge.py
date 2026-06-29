"""Aesthetic judge for the Charon reel — a vision scorer for the judge loop.

Renders a contact sheet of a clip's key frames, asks a vision model to score it
against videos/aesthetic_rubric.md, prints the score (so the quantitative judge
loop can parse it), and writes aesthetic_feedback.json next to the clip for the
implementer shade to read and act on.

    python videos/aesthetic_judge.py <slug>              # render sheet + score -> JSON
    python videos/aesthetic_judge.py <slug> --render     # just build the contact sheet
    python videos/aesthetic_judge.py <slug> --frames 6   # frames in the sheet

Vision backend (cloud opt-in; matches the reel's local-default/cloud-opt-in choice):
    CHARON_AESTHETIC_BACKEND = anthropic | openai   (auto-detected from keys)
    ANTHROPIC_API_KEY / OPENAI_API_KEY              (one required for live scoring)
    CHARON_AESTHETIC_MODEL                          (override; defaults per backend)
A local-vision backend (mlx-vlm) can be added here later as the offline default.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
RUBRIC = (REPO_ROOT / 'videos' / 'aesthetic_rubric.md').read_text(encoding='utf-8')
# Honor a named reel (set by /reel judge --reel <name>) so the judge scores the
# right clip; default is the built-in Charon reel under results/videos/.
_REEL = os.environ.get('CHARON_REEL_NAME', '').strip()
OUT_DIR = (REPO_ROOT / 'results' / 'videos') if not _REEL else (REPO_ROOT / 'results' / 'reels' / _REEL)

DEFAULT_MODELS = {'anthropic': 'claude-opus-4-8', 'openai': 'gpt-5.5'}


def clip_dir(slug: str) -> Path:
    return OUT_DIR / slug


def _ff() -> str:
    for c in ('/opt/homebrew/bin/ffmpeg', 'ffmpeg'):
        return c
    return 'ffmpeg'


def build_contact_sheet(slug: str, frames: int = 6) -> Path:
    """Tile N evenly-spaced frames of the clip into one image for scoring.

    Prefers final.mp4 (the real deliverable); falls back to any rendered still.
    """
    d = clip_dir(slug)
    sheet = d / 'aesthetic_sheet.png'
    video = d / 'final.mp4'
    if not video.exists():
        video = d / 'ui.mp4'
    if video.exists():
        cols = 2
        rows = (frames + cols - 1) // cols
        # Sample `frames` frames across the clip, scale, and tile.
        vf = (f"select='not(mod(n\\,{max(1, _frame_step(video, frames))}))',"
              f"scale=640:-1,tile={cols}x{rows}")
        subprocess.run([_ff(), '-y', '-i', str(video), '-vf', vf, '-frames:v', '1',
                        '-vsync', 'vfr', str(sheet)], check=True, capture_output=True)
        return sheet
    # Fallback: a single rendered still if present.
    stills = sorted(d.glob('media/images/**/*.png'))
    if stills:
        return stills[-1]
    raise FileNotFoundError(f'No final.mp4 or still to score for {slug} (in {d}).')


def _frame_step(video: Path, frames: int) -> int:
    """Pick a frame stride so ~`frames` frames are sampled across the clip."""
    try:
        out = subprocess.run(
            [_ff().replace('ffmpeg', 'ffprobe'), '-v', 'error', '-count_frames',
             '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames',
             '-of', 'default=nokey=1:noprint_wrappers=1', str(video)],
            capture_output=True, text=True, timeout=30,
        )
        total = int(out.stdout.strip() or '0')
        return max(1, total // max(1, frames))
    except Exception:
        return 30


def _openrouter_key() -> str:
    """OpenRouter key from env or the gitignored videos/.openrouter_key file."""
    k = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if k:
        return k
    f = REPO_ROOT / 'videos' / '.openrouter_key'
    if f.exists():
        return f.read_text(encoding='utf-8').strip()
    return ''


def _codex_available() -> bool:
    return shutil.which('codex') is not None


def _pick_backend() -> tuple[str, str, str] | None:
    """Return (backend, api_key, model) or None if no vision backend is configured.

    Prefers headless `codex` (uses the user's existing auth — no key, no extra
    cloud account) when available; falls back to OpenRouter / direct API keys.
    """
    backend = os.environ.get('CHARON_AESTHETIC_BACKEND', '').strip().lower()
    if not backend:
        if _codex_available():
            backend = 'codex'
        elif _openrouter_key():
            backend = 'openrouter'
        elif os.environ.get('ANTHROPIC_API_KEY'):
            backend = 'anthropic'
        elif os.environ.get('OPENAI_API_KEY'):
            backend = 'openai'
        else:
            return None
    if backend == 'codex':
        return backend, 'codex', os.environ.get('CHARON_AESTHETIC_MODEL', '')
    if backend == 'openrouter':
        key = _openrouter_key()
        model = os.environ.get('CHARON_AESTHETIC_MODEL') or 'anthropic/claude-sonnet-4'
    else:
        key = os.environ.get('ANTHROPIC_API_KEY' if backend == 'anthropic' else 'OPENAI_API_KEY', '')
        model = os.environ.get('CHARON_AESTHETIC_MODEL') or DEFAULT_MODELS.get(backend, '')
    if not key:
        return None
    return backend, key, model


def _score_with_codex(png: Path, prompt: str, model: str) -> dict:
    """Score a frame with headless `codex exec -i` (uses existing codex auth).

    Codex's sandbox can only read inside the project, so the image is copied
    under results/videos/ if it isn't already there.
    """
    img = png.resolve()
    if REPO_ROOT not in img.parents:
        img = (OUT_DIR / '_judge_input.png').resolve()
        img.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(png, img)
    out = OUT_DIR / '_judge_out.json'
    cmd = ['codex', 'exec', '-i', str(img.relative_to(REPO_ROOT)), '-o', str(out),
           f'An image is attached. {prompt}']
    if model:
        cmd[2:2] = ['-m', model]
    try:
        subprocess.run(cmd, cwd=str(REPO_ROOT), check=True, capture_output=True,
                       text=True, timeout=240)
        return _extract_json(out.read_text(encoding='utf-8'))
    except subprocess.CalledProcessError as e:
        return {'score': None, 'feedback': f'codex judge failed: {(e.stderr or "")[-200:]}',
                'error': 'codex_failed'}
    except Exception as e:
        return {'score': None, 'feedback': f'codex judge error: {e}', 'error': 'codex_error'}


def score_image(png: Path) -> dict:
    """Score a rendered frame/sheet against the rubric using a vision model."""
    chosen = _pick_backend()
    if chosen is None:
        return {'score': None, 'feedback': 'No vision backend configured. Set ANTHROPIC_API_KEY '
                'or OPENAI_API_KEY (and optionally CHARON_AESTHETIC_BACKEND/CHARON_AESTHETIC_MODEL).',
                'error': 'no_backend'}
    backend, key, model = chosen
    prompt = (f'{RUBRIC}\n\nScore the attached frame(s) now. Return ONLY the JSON object.')
    if backend == 'codex':
        return _score_with_codex(png, prompt, model)
    b64 = base64.standard_b64encode(png.read_bytes()).decode()
    try:
        if backend == 'anthropic':
            r = httpx.post(
                'https://api.anthropic.com/v1/messages',
                headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                         'content-type': 'application/json'},
                json={'model': model, 'max_tokens': 700, 'messages': [{'role': 'user', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': b64}},
                    {'type': 'text', 'text': prompt}]}]},
                timeout=90)
            r.raise_for_status()
            text = ''.join(b.get('text', '') for b in r.json().get('content', []))
        else:  # openai-compatible (openai or openrouter)
            base = ('https://openrouter.ai/api/v1' if backend == 'openrouter'
                    else os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'))
            r = httpx.post(
                f'{base}/chat/completions',
                headers={'authorization': f'Bearer {key}', 'content-type': 'application/json'},
                json={'model': model, 'max_tokens': 700, 'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}}]}]},
                timeout=90)
            r.raise_for_status()
            text = r.json()['choices'][0]['message']['content']
        return _extract_json(text)
    except Exception as e:
        return {'score': None, 'feedback': f'Vision call failed: {e}', 'error': 'call_failed'}


def _extract_json(text: str) -> dict:
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {'score': None, 'feedback': f'Unparseable judge output: {text[:300]}', 'error': 'parse'}


def judge(slug: str, frames: int = 6) -> dict:
    sheet = build_contact_sheet(slug, frames)
    verdict = score_image(sheet)
    (clip_dir(slug) / 'aesthetic_feedback.json').write_text(
        json.dumps(verdict, indent=2), encoding='utf-8')
    return verdict


def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__); return 2
    slug = argv[0]
    frames = 6
    if '--frames' in argv:
        frames = int(argv[argv.index('--frames') + 1])
    if '--render' in argv:
        print(build_contact_sheet(slug, frames)); return 0
    verdict = judge(slug, frames)
    # Print JSON last so the judge loop can parse {"score": N} (parse_field=score).
    print(json.dumps(verdict))
    return 0 if verdict.get('score') is not None else 1


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv[1:]))
