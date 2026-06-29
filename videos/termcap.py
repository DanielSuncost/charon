"""Real-TUI capture → brand-styled video, with zero external binaries.

Drives the actual Charon TUI inside a tmux pty, snapshots its colored screen
grid (`tmux capture-pane -e`) at each scripted beat, and rasterizes every frame
with PIL in the reel's font — producing crisp footage of REAL usage that drops
into the branded window frame. Uses only tmux + PIL + ffmpeg (all present).

    python videos/termcap.py            # record the default Charon walkthrough -> ui.mp4

A "step" is (keys, settle, hold):
  keys   : tmux send-keys argument(s) to send (or '' to just hold the screen)
  settle : seconds to wait after sending, before snapshotting (let the TUI repaint)
  hold   : seconds this frame stays on screen in the final video
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
MENLO = '/System/Library/Fonts/Menlo.ttc'

# Brand palette (mirrors brandkit.py) — the canvas the real text is drawn on.
BG = (7, 7, 8)
DEFAULT_FG = (232, 236, 241)
# Nudge a few common TUI colors toward the brand cyan/amber so capture + motion
# graphics feel like one piece. Truecolor values are otherwise honored as-is.
BRAND_REMAP = {
    (167, 139, 250): (34, 211, 238),   # charon header violet -> brand cyan
}

# Hi-res native render. 120x44 captures the FULL UI (mascot + embedded CHARON
# banner + welcome + input + status) and gives a near-16:9 aspect so the window
# fills the frame instead of sitting as a thin letterboxed band.
COLS, ROWS = 120, 44
FONT_PX = 54          # higher native res (~3.9K wide) for crisp downscale after tilt
PAD = 28
LINE_H = 1.30


def _font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(MENLO, FONT_PX)


def _cell_size(font) -> tuple[int, int]:
    # Monospace: measure a wide glyph; add leading for readability.
    box = font.getbbox('M')
    cw = font.getlength('M')
    ch = (box[3] - box[1])
    return int(round(cw)), int(round(ch * LINE_H))


# ── tmux capture ─────────────────────────────────────────────────────
def _tmux(*args: str) -> None:
    subprocess.run(['tmux', *args], check=False, capture_output=True)


def _snapshot(session: str) -> str:
    out = subprocess.run(['tmux', 'capture-pane', '-t', session, '-e', '-p'],
                         capture_output=True, text=True)
    return out.stdout


def record(steps: list, *, session: str = 'charon_reel',
           cols: int = COLS, rows: int = ROWS, launch: str = './charon',
           boot: float = 11.0, type_cps: float = 22.0) -> list[tuple[str, float]]:
    """Drive the real TUI and return [(ansi_frame, hold_seconds)] for a video.

    Each step is a dict:
      {'type': 'text'}            type text char-by-char (realistic), snapshot per char
      {'key':  'F2'}             send a special key once, snapshot
      {'hold': 2.0}              hold the current screen for N seconds (one frame)

    type_cps = characters per second (typing speed). A fast, realistic ~22 cps.
    """
    _tmux('kill-session', '-t', session)
    # Launch charon as the session's direct command — bypasses the interactive
    # shell (and oh-my-zsh's update prompt, which otherwise eats the keystrokes).
    _tmux('new-session', '-d', '-s', session, '-c', str(REPO_ROOT),
          '-x', str(cols), '-y', str(rows), launch)
    frames: list[tuple[str, float]] = []
    char_dt = 1.0 / type_cps
    try:
        time.sleep(boot)
        frames.append((_snapshot(session), 1.4))           # land on home
        for step in steps:
            if 'type' in step:
                for ch in step['type']:
                    _tmux('send-keys', '-t', session, '-l', ch)
                    time.sleep(char_dt)
                    frames.append((_snapshot(session), char_dt))
                if step.get('hold'):
                    frames.append((_snapshot(session), step['hold']))
            elif 'key' in step:
                _tmux('send-keys', '-t', session, step['key'])
                time.sleep(0.18)
                frames.append((_snapshot(session), step.get('hold', 0.8)))
            elif 'hold' in step:
                frames.append((_snapshot(session), step['hold']))
        return frames
    finally:
        _tmux('kill-session', '-t', session)


# ── ANSI grid -> PNG ─────────────────────────────────────────────────
_SGR = re.compile(r'\x1b\[([0-9;]*)m')


def _parse_line(line: str):
    """Yield (char, (r,g,b)) for a captured line, tracking SGR fg color."""
    fg = DEFAULT_FG
    bold = False
    i = 0
    while i < len(line):
        m = _SGR.match(line, i)
        if m:
            codes = [int(c) for c in m.group(1).split(';') if c != ''] or [0]
            j = 0
            while j < len(codes):
                c = codes[j]
                if c == 0:
                    fg, bold = DEFAULT_FG, False
                elif c == 1:
                    bold = True
                elif c == 39:
                    fg = DEFAULT_FG
                elif c == 38 and j + 1 < len(codes) and codes[j + 1] == 2:
                    fg = (codes[j + 2], codes[j + 3], codes[j + 4]); j += 4
                elif c == 38 and j + 1 < len(codes) and codes[j + 1] == 5:
                    fg = _xterm256(codes[j + 2]); j += 2
                elif 30 <= c <= 37:
                    fg = _ANSI16[c - 30]
                elif 90 <= c <= 97:
                    fg = _ANSI16[c - 90 + 8]
                j += 1
            i = m.end()
            continue
        ch = line[i]
        col = BRAND_REMAP.get(fg, fg)
        if bold and col == DEFAULT_FG:
            col = (255, 255, 255)
        yield ch, col
        i += 1


_ANSI16 = [
    (40, 42, 48), (255, 95, 87), (124, 227, 139), (255, 176, 32),
    (34, 211, 238), (167, 139, 250), (34, 211, 238), (232, 236, 241),
    (90, 100, 114), (255, 95, 87), (124, 227, 139), (255, 176, 32),
    (34, 211, 238), (167, 139, 250), (34, 211, 238), (255, 255, 255),
]


def _xterm256(n: int) -> tuple[int, int, int]:
    if n < 16:
        return _ANSI16[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return (v, v, v)
    n -= 16
    r, g, b = n // 36, (n % 36) // 6, n % 6
    s = [0, 95, 135, 175, 215, 255]
    return (s[r], s[g], s[b])


def render_frame(ansi_text: str, font, cw: int, ch: int) -> Image.Image:
    lines = ansi_text.split('\n')
    w = PAD * 2 + cw * COLS
    h = PAD * 2 + ch * ROWS
    img = Image.new('RGB', (w, h), BG)
    draw = ImageDraw.Draw(img)
    for row, line in enumerate(lines[:ROWS]):
        y = PAD + row * ch
        x = PAD
        for chq, col in _parse_line(line):
            if chq != ' ':
                draw.text((x, y), chq, font=font, fill=col)
            x += cw
    return img


def frames_to_video(frames: list[tuple[str, float]], out: Path, fps: int = 30) -> Path:
    """Rasterize frames and encode to a video honoring each frame's hold time."""
    work = out.parent / '_termcap_frames'
    work.mkdir(parents=True, exist_ok=True)
    font = _font()
    cw, ch = _cell_size(font)
    concat_lines = []
    for idx, (text, hold) in enumerate(frames):
        png = work / f'f{idx:04d}.png'
        render_frame(text, font, cw, ch).save(png)
        concat_lines.append(f"file '{png.as_posix()}'\nduration {max(0.1, hold):.3f}")
    # ffmpeg concat-demuxer needs the last image repeated to apply its duration.
    concat_lines.append(f"file '{(work / f'f{len(frames)-1:04d}.png').as_posix()}'")
    listing = work / 'frames.txt'
    listing.write_text('\n'.join(concat_lines), encoding='utf-8')
    subprocess.run(
        [FF, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing),
         '-vf', f'fps={fps},format=yuv420p', '-c:v', 'libx264', '-crf', '18', str(out)],
        check=True, capture_output=True)
    return out


# ── Default Charon walkthrough ───────────────────────────────────────
def default_steps() -> list:
    """A realistic, backend-free tour of the REAL TUI — typed live.

    Shows the command palette filtering to the new /reel and /skills features,
    then pane navigation. Nothing is executed (no LLM round-trips), so the
    footage is clean and deterministic — but the typing is live and fast.
    """
    return [
        {'hold': 1.6},                  # settle on the home screen (mascot)
        {'type': '/reel', 'hold': 2.4}, # palette filters to the reel commands — the money shot
        {'key': 'C-u', 'hold': 0.3},    # clear the input
        {'type': '/skills', 'hold': 2.2},   # palette shows bundled skills (manim-video)
        {'key': 'Escape', 'hold': 0.5},
        {'key': 'F2', 'hold': 1.9},     # dashboard pane
        {'key': 'F3', 'hold': 1.9},     # sessions pane
        {'key': 'F1', 'hold': 1.4},     # back to chat
    ]


def capture_ui(out: Path, steps=None, fps: int = 30) -> Path:
    frames = record(steps or default_steps())
    return frames_to_video(frames, out, fps=fps)


if __name__ == '__main__':
    import sys
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else (REPO_ROOT / 'results' / 'videos' / 'ui-capture.mp4')
    dest.parent.mkdir(parents=True, exist_ok=True)
    print('captured ->', capture_ui(dest))
