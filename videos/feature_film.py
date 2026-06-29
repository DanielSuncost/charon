"""Build ONE feature-focused film: editorial card + 3D-tilted UI + scripted voice.

Clean, dynamic, professional — the per-feature format. Reuses the editorial
brand system (Didot/hairlines), the 3D screen tilt/pan (tilt3d), and local
neural narration (mlx-audio).

    python videos/feature_film.py            # build the default feature
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VIDEOS = REPO / 'videos'
OUT = REPO / 'results' / 'videos'
WORK = OUT / '_feature'
FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
MANIM = str(REPO / '.venv' / 'bin' / 'manim')
PY = str(REPO / '.venv' / 'bin' / 'python3')
W, H, FPS = 1920, 1080, 30

sys.path.insert(0, str(VIDEOS))
from reel import GRADE_VF, _duration  # noqa: E402
import tilt3d  # noqa: E402

# ── The feature being filmed ─────────────────────────────────────────
FEATURE = {
    'number': '04', 'title': 'Making Videos', 'kicker': 'FEATURE',
    'subtitle': 'THE AGENTS BUILD THE FILM', 'index': 'MAKING VIDEOS',
    'outro_line': 'You describe the video once. The agents turn that into clips.',
}


def _script_lines(number: str) -> list[str]:
    """Pull the loop-approved narration for a feature from videos/scripts.md."""
    import re
    md = (VIDEOS / 'scripts.md').read_text(encoding='utf-8')
    lines, grab = [], False
    for ln in md.splitlines():
        if ln.startswith('## ') and f'{number} —' in ln:
            grab = True
            continue
        if ln.startswith('## ') and grab:
            break
        if grab:
            m = re.search(r'·\s*\*(.+?)\*\s*$', ln)
            if m:
                lines.append(m.group(1).strip())
    return lines


FEATURE['script'] = _script_lines(FEATURE['number'])

SCENES = '''\
from editorial import *

class Card(Scene):
    def construct(self):
        feature_card(self, number="%(number)s", title="%(title)s", kicker="%(kicker)s",
                     subtitle="%(subtitle)s", index="%(index)s", folio="%(number)s / 09", hold=0.7)
        card_out(self)

class Outro(Scene):
    def construct(self):
        outro(self, line="%(outro_line)s")
''' % FEATURE


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def render_cards() -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / 'editorial.py').write_text((VIDEOS / 'editorial.py').read_text())
    (WORK / 'scenes.py').write_text(SCENES)
    _run([MANIM, '-qh', '--media_dir', str(WORK / 'media'), str(WORK / 'scenes.py'),
          'Card', 'Outro'], cwd=str(WORK))
    q = WORK / 'media' / 'videos' / 'scenes' / '1080p60'
    return {'card': q / 'Card.mp4', 'outro': q / 'Outro.mp4'}


def make_body(ui_src: Path) -> Path:
    """Crop dead space, then 3D-tilt into the dynamic floating-screen body."""
    cropped = tilt3d.crop_to_content(ui_src, WORK / 'ui-cropped.mp4')
    return tilt3d.render(cropped, WORK / 'body.mp4', ui_w=1780, yaw0=0.11, yaw1=-0.07)


def _reencode(src: Path, dst: Path) -> Path:
    _run([FF, '-y', '-i', str(src), '-r', str(FPS), '-vf', f'scale={W}:{H},format=yuv420p',
          '-c:v', 'libx264', '-crf', '18', '-an', str(dst)])
    return dst


def concat(parts, out: Path) -> Path:
    norm = [_reencode(p, WORK / f'n{i}.mp4') for i, p in enumerate(parts)]
    lst = WORK / 'concat.txt'
    lst.write_text('\n'.join(f"file '{p.as_posix()}'" for p in norm))
    _run([FF, '-y', '-f', 'concat', '-safe', '0', '-i', str(lst), '-c', 'copy', str(out)])
    return out


def narrate(lines, out: Path) -> Path:
    import importlib
    hero = importlib.import_module('hero')
    hero.WORK = WORK
    hero.NARRATION = lines
    return hero.narrate(out)


def music_bed(seconds, out: Path) -> Path:
    import importlib
    hero = importlib.import_module('hero')
    hero.WORK = WORK
    return hero.music_bed(seconds, out)


def build(ui_src: Path) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    c = render_cards()
    body = make_body(ui_src)
    silent = concat([c['card'], body, c['outro']], WORK / 'silent.mp4')
    dur = _duration(silent)
    vo = narrate(FEATURE['script'], WORK / 'vo.mp3')
    bed = music_bed(dur + 0.5, WORK / 'music.mp3')
    graded = WORK / 'graded.mp4'
    _run([FF, '-y', '-i', str(silent), '-vf', GRADE_VF, '-c:v', 'libx264', '-crf', '19',
          '-pix_fmt', 'yuv420p', '-an', str(graded)])
    final = OUT / f"charon-feature-{FEATURE['number']}.mp4"
    _run([FF, '-y', '-i', str(graded), '-i', str(vo), '-i', str(bed),
          '-filter_complex',
          '[1:a]adelay=900|900,apad,asplit=2[voice][vkey];[2:a]volume=0.5[bed];'
          '[bed][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=320[duckm];'
          '[voice][duckm]amix=inputs=2:duration=first,loudnorm=I=-16:TP=-1.5:LRA=11[a]',
          '-map', '0:v', '-map', '[a]', '-r', str(FPS), '-vsync', 'cfr',
          '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', '-g', str(FPS * 2),
          '-c:a', 'aac', '-movflags', '+faststart', '-shortest', str(final)])
    return final


if __name__ == '__main__':
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else (WORK / 'ui-raw.mp4')
    print('built ->', build(src))
