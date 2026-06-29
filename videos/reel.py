"""Charon feature-reel driver.

Turns videos/features.yaml into a parallel SpawnBatch of video-producing
shades (each uses the manim-video skill + the shared brand kit), then stitches
the clips into one reel. Importable by the /reel command; also runnable
standalone for scaffolding/stitching without the daemon.

    python videos/reel.py list            # show manifest
    python videos/reel.py scaffold        # create per-video dirs + copy brandkit
    python videos/reel.py emit-batch      # print the SpawnBatch spec as JSON
    python videos/reel.py stitch          # concat clips -> results/videos/charon-reel.mp4
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
VIDEOS_DIR = REPO_ROOT / 'videos'
BRANDKIT = VIDEOS_DIR / 'brandkit.py'

# A "reel" is either the built-in Charon reel (REEL_NAME=None: manifest at
# videos/features.yaml, output under results/videos/) or a named reel for another
# app (videos/reels/<name>/features.yaml, output under results/reels/<name>/).
# /reel plan writes a named reel's manifest; set_reel() selects the active one.
REEL_NAME: str | None = None


def set_reel(name: str | None) -> None:
    global REEL_NAME
    REEL_NAME = name or None


def manifest_path() -> Path:
    return VIDEOS_DIR / 'features.yaml' if not REEL_NAME else VIDEOS_DIR / 'reels' / REEL_NAME / 'features.yaml'


def out_dir() -> Path:
    return (REPO_ROOT / 'results' / 'videos') if not REEL_NAME else (REPO_ROOT / 'results' / 'reels' / REEL_NAME)


def tapes_dir() -> Path:
    return VIDEOS_DIR / 'tapes' if not REEL_NAME else VIDEOS_DIR / 'reels' / REEL_NAME / 'tapes'


def reel_out() -> Path:
    return out_dir() / f'{REEL_NAME or "charon"}-reel.mp4'

# Color-safe cinematic finish: gentle vignette + slight contrast/saturation.
# Deliberately NO bloom screen-blend (shifts hue purple on dark frames) and NO
# grain (colored noise on near-black averages to a cast and bloats file size).
# The brand kit's in-scene glow already provides the bloom look.
GRADE_VF = 'eq=contrast=1.05:saturation=1.06,vignette=angle=PI/6'

# Music-under-narration mix: duck the bed when the voice is present (sidechain),
# blend, and normalize to broadcast loudness. Used when a music bed is supplied.
DUCK_FILTER = (
    '[1:a]volume=0.6[m];[0:a]asplit=2[v1][vsc];'
    '[m][vsc]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300[duck];'
    '[v1][duck]amix=inputs=2:duration=first:dropout_transition=0,'
    'loudnorm=I=-16:TP=-1.5:LRA=11[a]'
)


def load_manifest() -> dict:
    with manifest_path().open(encoding='utf-8') as f:
        return yaml.safe_load(f)


def ordered_features(slugs: list[str] | None = None) -> list[dict]:
    """Features in reel order, optionally filtered to a subset of slugs."""
    data = load_manifest()
    feats = {f['slug']: f for f in data.get('features', [])}
    order = data.get('reel', {}).get('order') or list(feats.keys())
    seq = [feats[s] for s in order if s in feats]
    # Append any features not listed in `order`.
    seq += [f for f in feats.values() if f['slug'] not in order]
    if slugs:
        want = set(slugs)
        seq = [f for f in seq if f['slug'] in want]
    return seq


def video_dir(slug: str) -> Path:
    return out_dir() / slug


def app_name() -> str:
    return load_manifest().get('reel', {}).get('app', 'Charon')


def reel_tagline() -> str:
    return load_manifest().get('reel', {}).get('tagline', '')


def scaffold(slug: str) -> Path:
    """Create the per-video dir and drop a fresh copy of the brand kit beside it.

    For a named reel, brand the copy with the app's wordmark/tagline so the
    intro/outro show the target app, not Charon.
    """
    d = video_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    kit = BRANDKIT.read_text(encoding='utf-8')
    reel = load_manifest().get('reel', {})
    wordmark = (reel.get('wordmark') or reel.get('app') or '').strip()
    if wordmark:
        kit = kit.replace('WORDMARK = "CHARON"', f'WORDMARK = "{wordmark.upper()}"')
    tagline = reel.get('tagline')
    if tagline:
        kit = kit.replace('tagline: str = "An agent OS for your local machine."',
                          f'tagline: str = "{tagline}"')
    (d / 'brandkit.py').write_text(kit, encoding='utf-8')
    return d


# ── Shared creative brief applied to EVERY task (the cohesion guarantee) ──
def brand_constraints() -> list[str]:
    return [
        'Use the manim-video skill: call Skills(view manim-video) and follow it.',
        'Import the shared brand kit: `from brandkit import *` (brandkit.py is in your working dir). '
        'Read it — use its primitives, do not reinvent them.',
        'FIRST scene calls brand_intro(self, title, tagline); LAST scene calls brand_outro(self).',
        'Start every content scene with gradient_bg(self) for depth (never a flat background).',
        'Frame UI/content in window_frame(...); put a glow(...) behind key solids; '
        'caption each beat with lower_third(self, "Heading", "detail"). Vary layout per scene.',
        'Use ONLY brand kit colors (PRIMARY, ACCENT, SECONDARY, MUTED, TEXT, PANEL) and the MONO font — no ad-hoc colors.',
        'Every animation gets a self.wait() after it; stagger reveals with LaggedStart; ease with rate_func=smooth.',
        'Render PRODUCTION quality: `manim -qh script.py <Scene1> <Scene2> ...` (1080p60). Iterate at -ql, deliver -qh.',
        'Inspect a still (`manim -qh -s ...`) and fix layout/timing/overlap BEFORE declaring done.',
        'Stitch scenes with ffmpeg into final.mp4 in your working directory (one file).',
        'Keep it tight — match the target seconds. Do not invent a different palette or wordmark.',
    ]


def instruction_for(feat: dict) -> str:
    points = '\n'.join(f'  - {p}' for p in feat.get('key_points', []))
    d = video_dir(feat['slug'])
    app = app_name()
    base = (
        f"Produce a ~{feat.get('seconds', 15)}s feature-highlight video for {app}'s "
        f"\"{feat['title']}\" capability.\n"
        f"Working directory: {d}\n"
        f"Title card text: \"{feat['title']}\"  subtitle: \"{feat.get('tagline', '')}\"\n"
        f"The video must convey these beats:\n{points}\n\n"
    )
    narration = feat.get('narration')
    if narration:
        script = 'Narration lines, in order (one per beat):\n' + '\n'.join(f'  - {n}' for n in narration)
    else:
        script = ('Write concise, warm spoken narration (one short line per beat) from the beats above.')
    base += (
        f"NARRATION (time-synced): make each content scene a CharonVoiceScene "
        f"(`from voice import CharonVoiceScene`), call self.setup_voice() first, and wrap each beat as "
        f"`with self.voiceover(text=LINE) as tr: self.play(..., run_time=tr.duration)` so the animation "
        f"length follows the speech. {script}\n\n"
    )
    if feat.get('capture'):
        tape = tapes_dir() / f"{feat['slug']}.tape"
        return base + (
            f"This feature uses REAL UI footage. Author {tape} "
            f"(copy videos/tapes/_template.tape) to drive the actual {app} UI into the "
            f"\"{feat['title']}\" view, then run `python videos/reel.py capture {feat['slug']}` "
            f"to record + fit it to ui.mp4. Render a brand_intro and brand_outro scene with manim, "
            f"and concat [intro, ui.mp4, outro] into final.mp4. Add a lower_third caption over the "
            f"UI if it helps. The body must show the real interface, not abstract shapes."
        )
    return base + (
        f"Pipeline: write plan.md, then script.py (one Scene class per beat, plus an intro scene "
        f"using brand_intro and an outro scene using brand_outro), render each scene with "
        f"`manim -qh`, then ffmpeg-concat into final.mp4. Inspect a still and fix layout/timing "
        f"before declaring done."
    )


def build_batch_spec(slugs: list[str] | None = None, max_concurrent: int = 4) -> dict:
    feats = ordered_features(slugs)
    for f in feats:
        scaffold(f['slug'])
    return {
        'goal': f'Produce a cohesive set of {app_name()} feature-highlight videos ({reel_tagline()})',
        'max_concurrent': max_concurrent,
        'constraints': brand_constraints(),
        'tasks': [
            {'title': f['title'], 'instruction': instruction_for(f), 'complexity': 'complex'}
            for f in feats
        ],
    }


MANIFEST_SCHEMA = """\
reel:
  app: "<App Name>"                 # shown in copy + clip intros
  wordmark: "<APP>"                 # short brand wordmark for the title cards
  tagline: "<one-line positioning>"
  order: [slug-a, slug-b, ...]      # clip order
  music: "<text prompt for the music bed>"
features:
  - slug: kebab-case-id             # output -> results/reels/<name>/<slug>/final.mp4
    title: "Feature Name"
    tagline: "one-line subtitle for the title card"
    seconds: 14                     # 12-18 reads best
    capture: false                  # true if a real UI screen-capture sells it better than animation
    key_points:                     # 3-5 concrete beats the clip must land (visualize, don't just caption)
      - "..."
    narration:                      # optional spoken lines, one per beat (else the shade writes them)
      - "..."
"""


def plan_instruction(target: str, name: str) -> str:
    """Brief for the director shade: study an app, write its reel manifest."""
    dest = manifest_path()
    return (
        f"You are the DIRECTOR for a product demo reel. Study the target app and decide the "
        f"marketing story, then WRITE the reel manifest. Do NOT render anything.\n\n"
        f"TARGET: {target}\n"
        f"(If this is a path, explore it: read README/docs, skim the UI/entry points, list real "
        f"features. If it's a description, work from that.)\n\n"
        f"Decide which {{6 to 9}} features are genuinely worth marketing — the ones a prospective "
        f"user cares about, not internal plumbing. Order them into a narrative (hook → depth → "
        f"payoff). For each, write a crisp title, a one-line tagline, 3-5 concrete visual beats, "
        f"and spoken narration. Mark capture:true only for features whose real on-screen UI sells "
        f"them better than animation.\n\n"
        f"Write valid YAML to: {dest}\n"
        f"Follow this schema exactly:\n\n{MANIFEST_SCHEMA}\n"
        f"After writing, summarize the chosen features and stop. The user will review the manifest "
        f"and then run the render."
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=nokey=1:noprint_wrappers=1', str(path)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def mix_music(video: Path, music: Path, out: Path) -> Path:
    """Duck a music bed under the video's existing narration and mux to `out`."""
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(video), '-i', str(music),
         '-filter_complex', DUCK_FILTER, '-map', '0:v', '-map', '[a]',
         '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(out)],
        check=True, capture_output=True)
    return out


def stitch(slugs: list[str] | None = None, grade: bool = True,
           music: str | Path | None = None) -> Path:
    """Concat each feature's final.mp4 (reel order), grade, then optional music.

    `music`: a path to a bed, or 'auto' to generate one (videos/music.py) sized to
    the reel and ducked under the clips' narration. None = leave narration as-is.
    """
    feats = ordered_features(slugs)
    clips = [video_dir(f['slug']) / 'final.mp4' for f in feats]
    have = [c for c in clips if c.exists()]
    missing = [f['slug'] for f, c in zip(feats, clips) if not c.exists()]
    if not have:
        raise FileNotFoundError(f'No clips rendered yet under {out_dir()}. Run the batch first.')
    out_dir().mkdir(parents=True, exist_ok=True)
    concat = out_dir() / 'concat.txt'
    concat.write_text(''.join(f"file '{c.as_posix()}'\n" for c in have), encoding='utf-8')
    raw = out_dir() / 'reel-raw.mp4'
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', str(concat), '-c', 'copy', str(raw)],
        check=True, capture_output=True,
    )
    # Grade video; copy audio so any narration survives.
    graded = out_dir() / 'reel-graded.mp4'
    if grade:
        subprocess.run(
            ['ffmpeg', '-y', '-i', str(raw), '-vf', GRADE_VF,
             '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', '-c:a', 'copy', str(graded)],
            check=True, capture_output=True,
        )
    else:
        shutil.copy2(raw, graded)

    if music:
        if str(music) == 'auto':
            import music as music_mod  # videos/ is on sys.path for the driver
            prompt = load_manifest().get('reel', {}).get('music', music_mod.DEFAULT_PROMPT)
            bed = music_mod.generate(prompt, _duration(graded) + 1, out_dir() / 'music.mp3')
        else:
            bed = Path(music)
        mix_music(graded, bed, reel_out())
    else:
        shutil.copy2(graded, reel_out())

    if missing:
        print(f'(stitched {len(have)} clips; still missing: {", ".join(missing)})')
    return reel_out()


FPS = 60
W, H = 1920, 1080


def capture(slug: str) -> Path:
    """Record the real Charon TUI for a feature via its VHS tape.

    Needs the `vhs` binary (`brew install vhs`). The tape lives at
    videos/tapes/<slug>.tape; Output is overridden to the feature's dir.
    """
    if shutil.which('vhs') is None:
        raise RuntimeError('vhs not installed. Run: brew install vhs')
    tape = tapes_dir() / f'{slug}.tape'
    if not tape.exists():
        raise FileNotFoundError(f'No tape for {slug} ({tape}). Copy videos/tapes/_template.tape.')
    d = scaffold(slug)
    out = d / 'capture.mp4'
    # Override Output so the tape's own Output line doesn't matter.
    subprocess.run(['vhs', str(tape), '--output', str(out)], check=True, cwd=str(d))
    return out


def fit_capture(slug: str, src: Path | None = None) -> Path:
    """Scale a real-UI capture onto a 1920x1080 brand-background frame -> ui.mp4.

    Re-encodes to match the manim clips (h264/yuv420p/60fps) so the intro,
    UI body, and outro concat cleanly. Centers the capture and pads with BG.
    """
    d = video_dir(slug)
    src = src or (d / 'capture.mp4')
    if not src.exists():
        raise FileNotFoundError(f'No capture for {slug} ({src}). Run capture first.')
    out = d / 'ui.mp4'
    vf = (f'scale={W}:{H}:force_original_aspect_ratio=decrease,'
          f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x070708,fps={FPS}')
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-vf', vf,
         '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', str(out)],
        check=True, capture_output=True,
    )
    return out


def grade_clip(slug: str) -> Path:
    """Apply the cinematic grade to a single feature's final.mp4 -> final_graded.mp4."""
    src = video_dir(slug) / 'final.mp4'
    if not src.exists():
        raise FileNotFoundError(f'No final.mp4 for {slug} ({src}).')
    out = video_dir(slug) / 'final_graded.mp4'
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-vf', GRADE_VF,
         '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', str(out)],
        check=True, capture_output=True,
    )
    return out


def _main(argv: list[str]) -> int:
    # Optional leading `--reel <name>` selects a named app reel.
    if argv and argv[0] == '--reel':
        set_reel(argv[1] if len(argv) > 1 else None)
        argv = argv[2:]
    cmd = argv[0] if argv else 'list'
    rest = argv[1:]
    if cmd == 'list':
        for f in ordered_features():
            print(f"{f['slug']:<20} {f['title']:<22} {f.get('seconds', '?')}s  — {f.get('tagline', '')}")
    elif cmd == 'scaffold':
        for f in ordered_features(rest or None):
            print('scaffolded', scaffold(f['slug']))
    elif cmd == 'emit-batch':
        print(json.dumps(build_batch_spec(rest or None), indent=2))
    elif cmd == 'stitch':
        music = None
        if '--music' in rest:
            i = rest.index('--music')
            music = rest[i + 1] if i + 1 < len(rest) and not rest[i + 1].startswith('-') else 'auto'
            rest = [r for r in rest if r != '--music' and r != music]
        print('reel:', stitch(rest or None, music=music))
    elif cmd == 'capture':
        for f in ordered_features(rest or None):
            try:
                print('captured', capture(f['slug']))
                print('fitted', fit_capture(f['slug']))
            except (RuntimeError, FileNotFoundError) as e:
                print('skip', f['slug'], '-', e)
    elif cmd == 'fit':
        for f in ordered_features(rest or None):
            try:
                print('fitted', fit_capture(f['slug']))
            except FileNotFoundError as e:
                print('skip', e)
    elif cmd == 'grade':
        for f in ordered_features(rest or None):
            try:
                print('graded', grade_clip(f['slug']))
            except FileNotFoundError as e:
                print('skip', e)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv[1:]))
