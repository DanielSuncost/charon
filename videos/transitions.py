"""Modular transitions for the production system.

Wraps ffmpeg's xfade into a named, composable transition library and chains a
list of clips into one timeline with a chosen transition between each pair —
returning each segment's start time on the composed timeline so narration and
music can be aligned to it.

    from transitions import chain, TRANSITIONS
    final, starts = chain([clipA, clipB, clipC], out, specs=[('dissolve',0.6),('fadeblack',0.5)])
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
FFPROBE = FF.replace('ffmpeg', 'ffprobe')

# name -> ffmpeg xfade transition. A curated, on-brand set.
TRANSITIONS = {
    'cut': None,                 # hard cut (no xfade)
    'fade': 'fade',              # crossfade through mix
    'dissolve': 'dissolve',      # grain dissolve
    'fadeblack': 'fadeblack',    # dip to black (editorial section break)
    'fadewhite': 'fadewhite',
    'wipeleft': 'wipeleft', 'wiperight': 'wiperight',
    'wipeup': 'wipeup', 'wipedown': 'wipedown',
    'slideleft': 'slideleft', 'slideright': 'slideright',
    'slideup': 'slideup', 'slidedown': 'slidedown',
    'smoothleft': 'smoothleft', 'smoothright': 'smoothright',
    'circleopen': 'circleopen', 'circleclose': 'circleclose',
    'radial': 'radial', 'pixelize': 'pixelize',
    'wipetl': 'wipetl', 'diagtl': 'diagtl',
}

DEFAULT = ('dissolve', 0.6)


def duration(path: Path) -> float:
    out = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=nokey=1:noprint_wrappers=1', str(path)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def chain(clips: list[Path], out: Path, *, specs: list[tuple[str, float]] | None = None,
          fps: int = 30) -> tuple[Path, list[float]]:
    """Chain clips into one video with a transition between each pair.

    specs[i] = (transition_name, duration) for the gap between clip i and i+1.
    Defaults to dissolve/0.6 for any unspecified gap. Returns (out, segment_starts)
    where segment_starts[i] is when clip i begins on the composed timeline.
    """
    clips = [Path(c) for c in clips]
    if len(clips) == 1:
        return clips[0], [0.0]
    specs = (specs or []) + [DEFAULT] * (len(clips) - 1 - len(specs or []))
    durs = [duration(c) for c in clips]

    inputs = []
    for c in clips:
        inputs += ['-i', str(c)]

    filt = []
    starts = [0.0]
    prev_label = '[0:v]'
    cum = durs[0]              # end of the composed timeline so far
    for i in range(1, len(clips)):
        name, tdur = specs[i - 1]
        xf = TRANSITIONS.get(name, 'dissolve')
        out_label = f'[v{i}]'
        if xf is None:         # hard cut == zero-duration concat-like join
            tdur = 0.0
            filt.append(f'{prev_label}[{i}:v]xfade=transition=fade:duration=0.001:'
                        f'offset={cum - 0.001:.3f}{out_label}')
        else:
            off = cum - tdur
            filt.append(f'{prev_label}[{i}:v]xfade=transition={xf}:duration={tdur}:'
                        f'offset={off:.3f}{out_label}')
        start = cum - tdur     # clip i visually begins as the transition starts
        starts.append(start)
        cum = start + durs[i]
        prev_label = out_label

    final_label = prev_label.strip('[]')
    subprocess.run([FF, '-y', *inputs, '-filter_complex', ';'.join(filt),
                    '-map', f'[{final_label}]', '-r', str(fps),
                    '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', str(out)],
                   check=True, capture_output=True)
    return out, starts


if __name__ == '__main__':
    print('transitions:', ', '.join(TRANSITIONS))
