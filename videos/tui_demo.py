"""Build a complete marketing video of the REAL Charon TUI.

Pipeline (all local, no external binaries beyond ffmpeg):
  1. record    the real TUI walkthrough via termcap (tmux capture -> PIL frames)
  2. intro/outro  branded motion-graphics bookends rendered with manim + brandkit
  3. fit       scale the footage onto the brand frame (1920x1080)
  4. narrate   spoken voiceover via the reel's voice backend (offline `say` default)
  5. music     a soft ambient bed, ducked under the narration
  6. grade     the cinematic finish, then mux -> results/videos/charon-tui-demo.mp4

    python videos/tui_demo.py            # build it
    python videos/tui_demo.py --no-record  # reuse an existing ui-capture.mp4
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VIDEOS = REPO_ROOT / 'videos'
OUT = REPO_ROOT / 'results' / 'videos'
WORK = OUT / '_tui_demo'
FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
VENV_MANIM = REPO_ROOT / '.venv' / 'bin' / 'manim'
W, H, FPS = 1920, 1080, 30

sys.path.insert(0, str(VIDEOS))
from reel import GRADE_VF, DUCK_FILTER, _duration  # noqa: E402

# Voiceover narration — describes what's on screen (roughly timeline-aligned).
NARRATION = (
    "This is Charon. An agent operating system that runs entirely on your machine. "
    "One terminal for every agent session. "
    "It even directs its own marketing videos, breaking an app into features and rendering each one. "
    "Bundled skills give agents real capabilities, like making animated video with Manim. "
    "Switch between chat, a live dashboard, and every running session. "
    "Charon. An agent OS for your local machine."
)

INTRO_OUTRO = '''\
from brandkit import *

class Intro(Scene):
    def construct(self):
        brand_intro(self, "Charon", "An agent OS for your local machine.")

class Outro(Scene):
    def construct(self):
        gradient_bg(self)
        mark = wordmark(1.0)
        mark_glow = behind_glow(mark, PRIMARY, pad=2.1, max_opacity=0.14)
        line = Text("Everything runs locally. You own the data.", font=MONO, color=ACCENT,
                    font_size=LABEL).next_to(mark, DOWN, buff=0.45)
        self.play(FadeIn(mark_glow), FadeIn(mark, scale=1.06), run_time=T_TITLE)
        self.play(FadeIn(line, shift=UP*0.2), run_time=0.8)
        self.wait(1.6)
        self.play(FadeOut(Group(mark, line, mark_glow)), run_time=0.6)
'''


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def record_ui() -> Path:
    import termcap
    WORK.mkdir(parents=True, exist_ok=True)
    raw = WORK / 'ui-raw.mp4'
    termcap.capture_ui(raw, fps=FPS)
    return raw


def fit_body(src: Path) -> Path:
    """Scale the real footage onto a 1920x1080 brand frame (centered, padded)."""
    out = WORK / 'body.mp4'
    vf = (f'scale={int(W*0.9)}:{int(H*0.9)}:force_original_aspect_ratio=decrease,'
          f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x070708,fps={FPS},format=yuv420p')
    _run([FF, '-y', '-i', str(src), '-vf', vf, '-c:v', 'libx264', '-crf', '18', str(out)])
    return out


def render_bookends() -> tuple[Path, Path]:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / 'brandkit.py').write_text((VIDEOS / 'brandkit.py').read_text())
    (WORK / 'bookends.py').write_text(INTRO_OUTRO)
    _run([str(VENV_MANIM), '-qh', '--media_dir', str(WORK / 'media'),
          str(WORK / 'bookends.py'), 'Intro', 'Outro'], cwd=str(WORK))
    q = WORK / 'media' / 'videos' / 'bookends' / '1080p60'
    return q / 'Intro.mp4', q / 'Outro.mp4'


def _reencode(src: Path, dst: Path) -> Path:
    """Normalize a clip to the concat target (h264/yuv420p/fps, silent)."""
    _run([FF, '-y', '-i', str(src), '-r', str(FPS), '-vf', f'scale={W}:{H},format=yuv420p',
          '-c:v', 'libx264', '-crf', '18', '-an', str(dst)])
    return dst


def concat(parts: list[Path], out: Path) -> Path:
    norm = [_reencode(p, WORK / f'n{i}.mp4') for i, p in enumerate(parts)]
    listing = WORK / 'concat.txt'
    listing.write_text('\n'.join(f"file '{p.as_posix()}'" for p in norm))
    _run([FF, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing), '-c', 'copy', str(out)])
    return out


def narrate(text: str, out: Path) -> Path:
    """Offline narration via macOS `say` -> mp3."""
    aiff = WORK / 'vo.aiff'
    voice = 'Samantha'
    _run(['say', '-v', voice, '-r', '172', '-o', str(aiff), text])
    _run([FF, '-y', '-i', str(aiff), str(out)])
    return out


def music_bed(seconds: float, out: Path) -> Path:
    """Synthesize a soft two-note ambient pad sized to the video."""
    # Detuned low sines + tremolo + gentle lowpass + long fades = unobtrusive bed.
    expr = (f"sine=frequency=110:duration={seconds},"
            f"aeval=val(0)*0.6:c=same")
    _run([FF, '-y',
          '-f', 'lavfi', '-i', f'sine=frequency=146.83:duration={seconds}',
          '-f', 'lavfi', '-i', f'sine=frequency=220:duration={seconds}',
          '-filter_complex',
          '[0:a]volume=0.5[a0];[1:a]volume=0.35[a1];'
          '[a0][a1]amix=inputs=2,tremolo=f=0.2:d=0.3,lowpass=f=900,'
          f'afade=t=in:st=0:d=2,afade=t=out:st={max(0,seconds-2.5):.2f}:d=2.5,volume=0.5[m]',
          '-map', '[m]', '-c:a', 'libmp3lame', '-q:a', '4', str(out)])
    return out


def build(record: bool = True) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    body_src = record_ui() if record else (WORK / 'ui-raw.mp4')
    if not body_src.exists():
        raise FileNotFoundError(f'{body_src} missing — run without --no-record first.')
    body = fit_body(body_src)
    intro, outro = render_bookends()
    silent = concat([intro, body, outro], WORK / 'silent.mp4')

    dur = _duration(silent)
    vo = narrate(NARRATION, WORK / 'vo.mp3')
    bed = music_bed(dur + 0.5, WORK / 'music.mp3')

    # Voice starts ~0.8s in (after the title lands); music underneath, ducked.
    final = OUT / 'charon-tui-demo.mp4'
    graded = WORK / 'graded.mp4'
    _run([FF, '-y', '-i', str(silent), '-vf', GRADE_VF, '-c:v', 'libx264', '-crf', '19',
          '-pix_fmt', 'yuv420p', '-an', str(graded)])
    # [0]=graded video, [1]=voice (delayed), [2]=music -> duck music under voice, mux.
    _run([FF, '-y', '-i', str(graded), '-i', str(vo), '-i', str(bed),
          '-filter_complex',
          '[1:a]adelay=800|800,apad,asplit=2[voice][vkey];'
          '[2:a]volume=0.5[bed];'
          '[bed][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=320[duckm];'
          '[voice][duckm]amix=inputs=2:duration=first,loudnorm=I=-16:TP=-1.5:LRA=11[a]',
          '-map', '0:v', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(final)])
    return final


if __name__ == '__main__':
    out = build(record='--no-record' not in sys.argv)
    print('built ->', out)
