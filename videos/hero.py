"""Build the Charon HERO video — editorial design + real hi-res TUI footage.

Structure:
  INTRO (editorial)  →  FEATURE CARD "The Interface"  →  real TUI in an
  editorial frame (masthead + caption)  →  OUTRO (editorial)
with narration (mlx-audio if built, else best local voice) + music + grade.

    python videos/hero.py              # full build
    python videos/hero.py --no-record  # reuse the last hi-res capture
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VIDEOS = REPO / 'videos'
OUT = REPO / 'results' / 'videos'
WORK = OUT / '_hero'
FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
MANIM = REPO / '.venv' / 'bin' / 'manim'
MLX_TTS = REPO.parent / 'mlx-audio-swift' / '.build' / 'release' / 'mlx-audio-swift-tts'
W, H, FPS = 1920, 1080, 30

sys.path.insert(0, str(VIDEOS))
from reel import GRADE_VF, _duration  # noqa: E402

NARRATION = [
    "This is Charon — an agent operating system that runs entirely on your machine.",
    "One terminal commands every agent you run.",
    "It even directs its own marketing videos — breaking an app into features and rendering each one.",
    "Bundled skills give agents real capabilities, like producing animated video.",
    "Chat, a live dashboard, and every running session — one screen.",
    "Charon. Everything runs locally. You own the data.",
]

# ── manim bookends + editorial frame plate (generated scene file) ─────
SCENES = '''\
from editorial import *

class Intro(Scene):
    def construct(self):
        intro(self)

class Card(Scene):
    def construct(self):
        feature_card(self, number="01", title="The Interface", kicker="CHARON",
                     subtitle="ONE TERMINAL — EVERY AGENT",
                     index="THE INTERFACE", folio="01 / 09", hold=0.6)
        card_out(self)

class Outro(Scene):
    def construct(self):
        outro(self)
'''


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ── Near-full-bleed editorial plate (PIL, pixel-exact) ───────────────
# The TUI window is the hero — it fills the frame so the whole mascot reads.
# Editorial chrome moves to the side margins (vertical tracked labels) since
# there's no room above/below a near-full window.
WIN_W, WIN_H = 1754, 1040      # ~1.686 aspect (matches the 120x44 capture)
WIN_X, WIN_Y = (W - WIN_W) // 2, (H - WIN_H) // 2   # centered, ~83px side bars
PAL = {'bg': (11, 10, 9), 'paper': (236, 231, 222), 'mute': (133, 124, 112),
       'faint': (58, 53, 46), 'accent': (224, 133, 42)}
HELV = '/System/Library/Fonts/HelveticaNeue.ttc'


def _tracked(draw, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing
    return x


def _vlabel(text, size, fill, spacing, ccw=True):
    """A rotated tracked label image (for the side margins)."""
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype(HELV, size)
    w = int(sum(ImageDraw.Draw(Image.new('RGB', (1, 1))).textlength(c, font=f) + spacing for c in text))
    im = Image.new('RGBA', (w + 8, size + 10), (0, 0, 0, 0))
    _tracked(ImageDraw.Draw(im), (0, 2), text, f, fill, spacing)
    return im.rotate(90 if ccw else -90, expand=True)


def make_plate(out: Path) -> Path:
    from PIL import Image
    img = Image.new('RGB', (W, H), PAL['bg'])
    # Left margin: vertical masthead. Right margin: vertical caption + folio.
    left = _vlabel('CHARON', 22, PAL['paper'], 6, ccw=True)
    img.paste(left, ((WIN_X - left.width) // 2, (H - left.height) // 2), left)
    right = _vlabel('THE INTERFACE — LIVE COMMAND PALETTE', 14, PAL['accent'], 4, ccw=False)
    rx = WIN_X + WIN_W + (WIN_X - right.width) // 2
    img.paste(right, (rx, (H - right.height) // 2), right)
    img.save(out)
    return out


def render_manim() -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / 'editorial.py').write_text((VIDEOS / 'editorial.py').read_text())
    (WORK / 'scenes.py').write_text(SCENES)
    _run([str(MANIM), '-qh', '--media_dir', str(WORK / 'media'), str(WORK / 'scenes.py'),
          'Intro', 'Card', 'Outro'], cwd=str(WORK))
    q = WORK / 'media' / 'videos' / 'scenes' / '1080p60'
    plate_png = make_plate(WORK / 'plate.png')
    return {'intro': q / 'Intro.mp4', 'card': q / 'Card.mp4',
            'outro': q / 'Outro.mp4', 'plate': plate_png}


def record_ui() -> Path:
    import termcap
    WORK.mkdir(parents=True, exist_ok=True)
    raw = WORK / 'ui-raw.mp4'
    termcap.capture_ui(raw, fps=FPS)
    return raw


def composite_tui(ui: Path, plate: Path) -> Path:
    """Overlay the hi-res TUI capture large, at the exact editorial window rect."""
    out = WORK / 'tui.mp4'
    _run([FF, '-y', '-loop', '1', '-i', str(plate), '-i', str(ui),
          '-filter_complex',
          f'[1:v]scale={WIN_W}:{WIN_H}[ui];[0:v][ui]overlay={WIN_X}:{WIN_Y}:shortest=1,'
          f'fps={FPS},format=yuv420p[v]',
          '-map', '[v]', '-c:v', 'libx264', '-crf', '18', str(out)])
    return out


def _reencode(src: Path, dst: Path) -> Path:
    _run([FF, '-y', '-i', str(src), '-r', str(FPS), '-vf', f'scale={W}:{H},format=yuv420p',
          '-c:v', 'libx264', '-crf', '18', '-an', str(dst)])
    return dst


def concat(parts: list[Path], out: Path) -> Path:
    norm = [_reencode(p, WORK / f'n{i}.mp4') for i, p in enumerate(parts)]
    listing = WORK / 'concat.txt'
    listing.write_text('\n'.join(f"file '{p.as_posix()}'" for p in norm))
    _run([FF, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing), '-c', 'copy', str(out)])
    return out


# ── audio ────────────────────────────────────────────────────────────
PY = str(REPO / '.venv' / 'bin' / 'python3')
MLX_MODEL = 'mlx-community/Kokoro-82M-bf16'     # local neural TTS
MLX_VOICE = 'bm_lewis'                          # British male, measured/austere


def _kokoro_once(text: str, prefix: Path) -> Path | None:
    try:
        _run([PY, '-m', 'mlx_audio.tts.generate', '--model', MLX_MODEL,
              '--voice', MLX_VOICE, '--text', text, '--file_prefix', str(prefix)])
        wav = Path(f'{prefix}_000.wav')
        return wav if wav.exists() else None
    except subprocess.CalledProcessError:
        return None


def _silences(wav: Path) -> list[tuple[float, float]]:
    p = subprocess.run([FF, '-i', str(wav), '-af', 'silencedetect=noise=-35dB:d=0.16',
                        '-f', 'null', '-'], capture_output=True, text=True)
    starts, ends = [], []
    for ln in p.stderr.splitlines():
        if 'silence_start' in ln:
            starts.append(float(ln.split('silence_start:')[1].strip()))
        elif 'silence_end' in ln:
            ends.append(float(ln.split('silence_end:')[1].split('|')[0].strip()))
    return list(zip(starts, ends + [None] * (len(starts) - len(ends))))


def _adur(wav: Path) -> float:
    out = subprocess.run([FF.replace('ffmpeg', 'ffprobe'), '-v', 'error', '-show_entries',
                          'format=duration', '-of', 'default=nokey=1:noprint_wrappers=1', str(wav)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _trim_ends(wav: Path, out: Path) -> Path:
    """Trim ONLY leading/trailing silence via silencedetect+atrim — never cuts
    mid-content (silenceremove was eating quiet word edges = skipped words)."""
    sil = _silences(wav)
    dur = _adur(wav)
    start = sil[0][1] if (sil and sil[0][0] <= 0.06 and sil[0][1] is not None) else 0.0
    end = None
    if sil:
        ls, le = sil[-1]
        if le is None or abs(le - dur) < 0.12:   # last region runs to EOF = trailing silence
            end = ls
    a = f'atrim=start={max(0, start - 0.03):.3f}'
    if end and end > start + 0.2:
        a += f':end={end + 0.10:.3f}'
    a += ',asetpts=PTS-STARTPTS'
    _run([FF, '-y', '-i', str(wav), '-af', a, '-ar', '24000', '-ac', '1', str(out)])
    return out


def _concat_wavs(wavs: list[Path], gap_s: float, out: Path) -> Path | None:
    gap = out.with_name(out.stem + '_gap.wav')
    _run([FF, '-y', '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono', '-t', f'{gap_s}', str(gap)])
    seq = []
    for w in wavs:
        seq += [w, gap]
    listing = out.with_name(out.stem + '_list.txt')
    listing.write_text('\n'.join(f"file '{p.resolve().as_posix()}'" for p in seq[:-1]))
    try:
        _run([FF, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing), '-ar', '24000', '-ac', '1', str(out)])
        return out if out.exists() else None
    except subprocess.CalledProcessError:
        return None


def _say_clause(text: str, prefix: Path) -> Path | None:
    """Synthesize a short clause. On Kokoro's tensor bug, split the words in half
    and synthesize each — this PRESERVES every word (no trimming, no dropping)."""
    w = _kokoro_once(text, prefix)
    if w:
        return w
    words = text.split()
    if len(words) < 2:
        return None
    mid = len(words) // 2
    a = _say_clause(' '.join(words[:mid]), Path(f'{prefix}a'))
    b = _say_clause(' '.join(words[mid:]), Path(f'{prefix}b'))
    if a is None or b is None:
        return None
    return _concat_wavs([a, b], 0.08, Path(f'{prefix}_000.wav'))


def _say_one(text: str, prefix: Path) -> Path | None:
    """One sentence. Try whole; on failure split by commas (then words) so all
    words survive. Kokoro rushes long inputs, so commas also help prosody."""
    w = _kokoro_once(text, prefix)
    if w:
        return w
    clauses = [c.strip() for c in text.replace(';', ',').split(',') if c.strip()]
    if len(clauses) <= 1:
        return _say_clause(text, prefix)
    cwavs = []
    for k, c in enumerate(clauses):
        cw = _say_clause(c, Path(f'{prefix}_c{k}'))
        if cw is None:
            return None
        cwavs.append(cw)
    return _concat_wavs(cwavs, 0.12, Path(f'{prefix}_000.wav'))


def _mlx_say(text: str, prefix: Path) -> Path | None:
    """Synthesize a narration line: split into sentences, voice each, trim only the
    end-silence of each, join with one natural pause. Preserves every word."""
    import re
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
    trimmed = []
    for j, s in enumerate(sentences):
        w = _say_one(s, Path(f'{prefix}_s{j}'))
        if w is None:
            return None
        trimmed.append(_trim_ends(w, Path(f'{prefix}_t{j}.wav')))
    if len(trimmed) == 1:
        out = Path(f'{prefix}_000.wav')
        _run([FF, '-y', '-i', str(trimmed[0]), '-ar', '24000', '-ac', '1', str(out)])
        return out
    return _concat_wavs(trimmed, 0.30, Path(f'{prefix}_000.wav'))


def narrate(out: Path) -> Path:
    """Voice the narration with local neural mlx-audio (per line), else `say`.

    Each sentence is synthesized separately for natural pauses, then concatenated.
    """
    parts = []
    for i, line in enumerate(NARRATION):
        w = _mlx_say(line, WORK / f'vo{i}')
        if w is None:
            parts = []
            break
        parts.append(w)
    if parts:
        # concat the per-line wavs with a short gap between sentences
        gap = WORK / 'gap.wav'
        _run([FF, '-y', '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono', '-t', '0.35', str(gap)])
        listing = WORK / 'vo_list.txt'
        seq = []
        for p in parts:
            seq.append(f"file '{p.as_posix()}'")
            seq.append(f"file '{gap.as_posix()}'")
        listing.write_text('\n'.join(seq))
        _run([FF, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing), str(out)])
        return out
    # Fallback: macOS `say`.
    aiff = WORK / 'vo.aiff'
    _run(['say', '-v', 'Daniel', '-r', '170', '-o', str(aiff), ' '.join(NARRATION)])
    _run([FF, '-y', '-i', str(aiff), str(out)])
    return out


def music_bed(seconds: float, out: Path) -> Path:
    """An evolving ambient drone — a detuned A-minor-ish chord with gentle beating,
    a faint 9th for avant-garde color, independent slow swells per voice, and light
    reverb. Moves slowly and organically without sounding jarring or fake."""
    d = seconds
    # Tuned to Om — 136.1 Hz (C#) is the traditional meditation tone. The tonic drone
    # sits an octave below (68 Hz) so the bed is deep, with the recognizable Om pitch
    # present and clear, plus a faint ninth/third for avant-garde color.
    # (frequency, volume, tremolo Hz, tremolo depth) — detuned, slowly swelling voices.
    voices = [
        (34.02, 0.22, 0.07, 0.30),   # C#0 — deep sub, warmth/body
        (68.05, 0.48, 0.05, 0.35),   # C#1 — Om, octave-down tonic (the root drone)
        (68.34, 0.30, 0.08, 0.40),   # +7c — slow beating against the tonic
        (102.08, 0.28, 0.06, 0.35),  # G#1 — fifth
        (81.66, 0.14, 0.10, 0.50),   # E1 — minor third, color
        (136.10, 0.34, 0.06, 0.40),  # C#2 — THE Om note, present and clear
        (153.11, 0.10, 0.11, 0.55),  # D#2 — ninth, faint avant-garde tension
        (204.15, 0.13, 0.09, 0.45),  # G#2 — fifth shimmer
        (272.20, 0.08, 0.13, 0.60),  # C#3 — high octave shimmer, swelling
    ]
    inputs = []
    for f, *_ in voices:
        inputs += ['-f', 'lavfi', '-i', f'sine=frequency={f:.2f}:duration={d:.2f}']
    chains = []
    for i, (f, vol, tf, td) in enumerate(voices):
        # tremolo min freq is 0.1 Hz; clamp and let detune-beating carry the slowest motion
        chains.append(f'[{i}:a]volume={vol},tremolo=f={max(0.1, tf):.2f}:d={td}[a{i}]')
    mix = ''.join(f'[a{i}]' for i in range(len(voices)))
    fade_out = max(0, d - 3.0)
    post = (f'{mix}amix=inputs={len(voices)}:normalize=0,'
            'lowpass=f=1100,highpass=f=40,'           # warm band, no harsh highs/rumble
            'aecho=0.8:0.85:160|220:0.28|0.2,'        # light reverb-ish space
            'tremolo=f=0.1:d=0.12,'                   # whole-bed slow breath
            f'afade=t=in:st=0:d=3,afade=t=out:st={fade_out:.2f}:d=3,'
            'volume=0.42,'
            'loudnorm=I=-23:TP=-3:LRA=14[m]')         # quiet, wide-dynamic bed
    _run([FF, '-y', *inputs, '-filter_complex', ';'.join(chains) + ';' + post,
          '-map', '[m]', '-c:a', 'libmp3lame', '-q:a', '4', str(out)])
    return out


def build(record: bool = True) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    m = render_manim()
    ui_src = record_ui() if record else (WORK / 'ui-raw.mp4')
    if not ui_src.exists():
        raise FileNotFoundError(f'{ui_src} missing — run without --no-record first.')
    tui = composite_tui(ui_src, m['plate'])
    silent = concat([m['intro'], m['card'], tui, m['outro']], WORK / 'silent.mp4')

    dur = _duration(silent)
    vo = narrate(WORK / 'vo.mp3')
    bed = music_bed(dur + 0.5, WORK / 'music.mp3')

    graded = WORK / 'graded.mp4'
    _run([FF, '-y', '-i', str(silent), '-vf', GRADE_VF, '-c:v', 'libx264', '-crf', '19',
          '-pix_fmt', 'yuv420p', '-an', str(graded)])
    final = OUT / 'charon-hero.mp4'
    # Robust, broadly-playable encode: constant frame rate, yuv420p, +faststart
    # (so players don't stall mid-clip on a missing moov atom / VFR timestamps).
    _run([FF, '-y', '-i', str(graded), '-i', str(vo), '-i', str(bed),
          '-filter_complex',
          '[1:a]adelay=900|900,apad,asplit=2[voice][vkey];'
          '[2:a]volume=0.5[bed];'
          '[bed][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=320[duckm];'
          '[voice][duckm]amix=inputs=2:duration=first,loudnorm=I=-16:TP=-1.5:LRA=11[a]',
          '-map', '0:v', '-map', '[a]', '-r', str(FPS), '-vsync', 'cfr',
          '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', '-g', str(FPS * 2),
          '-c:a', 'aac', '-movflags', '+faststart', '-shortest', str(final)])
    return final


if __name__ == '__main__':
    print('built ->', build(record='--no-record' not in sys.argv))
