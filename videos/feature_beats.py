"""Per-beat feature film: each narration line gets its own focused, tilted UI shot.

This replaces the single-walkthrough body (which the visual judge capped at 7.2
because it read as a screenshot) with deliberate per-beat shots: the /reel plan
command, the dashboard with shades running, the judge command — each cropped to
its subject, made into a still, and tilted/panned, timed to its narration line.

    python videos/feature_beats.py
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
import numpy as np

REPO = Path(__file__).resolve().parents[1]
VIDEOS = REPO / 'videos'
OUT = REPO / 'results' / 'videos'
WORK = OUT / '_beats'
FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
MANIM = str(REPO / '.venv' / 'bin' / 'manim')
W, H, FPS = 1920, 1080, 30

sys.path.insert(0, str(VIDEOS))
import termcap as tc
import tilt3d
import transitions
import render_panel as rp

from reel import GRADE_VF, _duration

hero = importlib.import_module('hero')
hero.WORK = WORK

# ── Feature registry — one config per feature video. Select with CHARON_FEATURE ──
# Each beat: {'kind':'panel', 'yaw':..., 'panel':{...render_panel kwargs...}} or
#            {'kind':'scene', 'name':..., 'call':'<diagram fn call on self>'}.
FEATURES = {
    '04': {
        'number': '04', 'title': 'Making Videos',
        'thesis': 'HOW CHARON WRITES, RENDERS, AND JUDGES ITS OWN FILMS',
        'intro_line': 'Charon makes its own videos. It plans them, renders each part, and checks the result.',
        'outro_line': 'You describe the video once. The agents turn that into clips.',
        'transitions': [('fadeblack', 0.6), ('dissolve', 0.6), ('dissolve', 0.6), ('fadeblack', 0.5)],
        'beats': [
            {'kind': 'panel', 'yaw': (0.42, -0.30), 'panel': dict(
                filename='features.yaml', label='THE DIRECTOR — PROPOSED MANIFEST',
                caption='Reads the project and proposes the features to show.',
                syntax='yaml', folio='01', highlight=2,
                content=('reel:\n  app: "Charon"\n  order: [memory, shades, judge-loops, video]\n'
                         'features:\n  - slug: memory\n    title: "Memory"\n    seconds: 15\n'
                         '  - slug: shades\n    title: "Shades"\n    seconds: 15\n'
                         '  - slug: video\n    title: "Making Videos"\n    seconds: 15'))},
            {'kind': 'scene', 'name': 'Swarm',
             'call': 'swarm_scene(self, children=["shade · plan", "shade · render", "shade · audio", "shade · judge"], hold=1.4)'},
            {'kind': 'scene', 'name': 'JudgeLoop', 'call': 'loop_scene(self, hold=1.4)'},
        ],
    },
    'memory': {
        'number': '01', 'title': 'Memory',
        'thesis': 'CONTEXT SAVED ON YOUR MACHINE, FOUND AGAIN BY MEANING',
        'intro_line': 'Charon keeps useful context on your machine, and finds it again by meaning.',
        'outro_line': 'The memory stays on your machine, and you can inspect it.',
        'transitions': [('fadeblack', 0.6), ('dissolve', 0.6), ('dissolve', 0.6), ('dissolve', 0.6), ('fadeblack', 0.5)],
        'beats': [
            {'kind': 'scene', 'name': 'Pipeline',
             'call': 'pipeline_scene(self, stages=["conversation", "embed by meaning", "local db", "ranked recall"], '
                     'kicker="THE STORE", caption="SAVED BY MEANING, SEARCHED BY MEANING", folio="01 / 09", hold=1.5)'},
            {'kind': 'scene', 'name': 'Local',
             'call': 'local_scene(self, hold=1.5)'},
            {'kind': 'scene', 'name': 'Ranked',
             'call': 'ranked_scene(self, rows=[("the thread you meant", 0.96), ("a related note", 0.7), '
                     '("older context", 0.5), ("unrelated", 0.26)], '
                     'caption="THE RIGHT RESULT IS USUALLY NEAR THE TOP", folio="01 / 09", hold=1.5)'},
            {'kind': 'scene', 'name': 'Across',
             'call': 'fanout_scene(self, source="a saved preference", '
                     'targets=["project · alpha", "project · beta", "project · gamma"], '
                     'kicker="ACROSS PROJECTS", caption="A PREFERENCE SAVED ONCE CAN BE USED ELSEWHERE", '
                     'folio="01 / 09", hold=1.5)'},
        ],
    },
    'judge': {
        'number': '03', 'title': 'Judge Loops',
        'thesis': 'REPEAT A TASK WHILE A SCORE KEEPS IMPROVING',
        'intro_line': 'Charon can improve a task by repeating it, whenever there is a score to chase.',
        'outro_line': 'The loop keeps the best result it has found.',
        'transitions': [('fadeblack', 0.6), ('dissolve', 0.6), ('dissolve', 0.6), ('fadeblack', 0.5)],
        'beats': [
            {'kind': 'scene', 'name': 'Loop',
             'call': 'pipeline_scene(self, stages=["snapshot", "change files", "score", "compare"], '
                     'kicker="THE LOOP", caption="SNAPSHOT · CHANGE · SCORE · COMPARE", folio="03 / 09", hold=1.5)'},
            {'kind': 'scene', 'name': 'Branch',
             'call': 'branch_scene(self, hold=1.5)'},
            {'kind': 'scene', 'name': 'Kinds',
             'call': 'fanout_scene(self, source="the score", '
                     'targets=["a benchmark number", "a passing test", "a model + rubric"], '
                     'kicker="WHAT THE SCORE CAN BE", caption="A NUMBER, A TEST, OR A MODEL RATING A RUBRIC", '
                     'folio="03 / 09", hold=1.5)'},
        ],
    },
    'fleet': {
        'number': '05', 'title': 'The Fleet',
        'thesis': 'EVERY SESSION, LOCAL AND REMOTE, ON ONE SCREEN',
        'intro_line': 'Charon puts every agent session, local or remote, on one screen.',
        'outro_line': 'All active sessions are visible on one screen.',
        'transitions': [('fadeblack', 0.6), ('dissolve', 0.6), ('fadeblack', 0.5)],
        'beats': [
            {'kind': 'scene', 'name': 'Grid',
             'call': 'grid_scene(self, hold=1.6)'},
            {'kind': 'scene', 'name': 'Dispatch',
             'call': 'dispatch_scene(self, hold=1.6)'},
        ],
    },
    'shades': {
        'number': '02', 'title': 'Shades',
        'thesis': 'PARALLEL WORKERS, EACH KEPT INSIDE ITS LANE',
        'intro_line': 'Charon can split work across many agents at once. Each one is called a shade.',
        'outro_line': 'Many workers, each with a job, a budget, and a file boundary.',
        'transitions': [('fadeblack', 0.6), ('dissolve', 0.6), ('dissolve', 0.6), ('fadeblack', 0.5)],
        'beats': [
            {'kind': 'scene', 'name': 'Swarm',
             'call': 'swarm_scene(self, children=["shade · 01", "shade · 02", "shade · 03", "shade · 04"], '
                     'kicker="THE FAN-OUT", caption="ONE AGENT STARTS MANY, RUNNING AT ONCE", folio="02 / 09", hold=1.4)'},
            {'kind': 'scene', 'name': 'Contract',
             'call': 'contract_scene(self, hold=1.4)'},
            {'kind': 'scene', 'name': 'Budget',
             'call': 'budget_scene(self, hold=1.4)'},
        ],
    },
}

FEATURE = FEATURES[os.environ.get('CHARON_FEATURE', '04')]


def _gen_scenes(cfg: dict) -> str:
    src = ['from editorial import *', 'from diagram import *',
           'class VideoIntro(Scene):',
           '    def construct(self):',
           f'        video_intro(self, title="{cfg["title"]}", thesis="{cfg["thesis"]}", '
           f'number="{cfg["number"]}", hold=2.8)']
    for b in cfg['beats']:
        if b['kind'] == 'scene':
            src += [f'class {b["name"]}(Scene):', '    def construct(self):', f'        {b["call"]}']
    src += ['class Outro(Scene):', '    def construct(self):',
            f'        outro(self, line="{cfg["outro_line"]}")']
    return '\n'.join(src)


BEATS = FEATURE['beats']
TRANSITION_SPECS = FEATURE['transitions']
SCENES = _gen_scenes(FEATURE)


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def _tmux(*a):
    subprocess.run(['tmux', *a], capture_output=True)


def _capture_dashboard() -> Path:
    """Capture the real dashboard pane (shades running) at hi-res, cropped."""
    WORK.mkdir(parents=True, exist_ok=True)
    _tmux('kill-session', '-t', 'beats')
    _tmux('new-session', '-d', '-s', 'beats', '-c', str(REPO), '-x', '120', '-y', '44', './charon')
    time.sleep(12)
    _tmux('send-keys', '-t', 'beats', 'F2'); time.sleep(1.4)
    ansi = subprocess.run(['tmux', 'capture-pane', '-t', 'beats', '-e', '-p'],
                          capture_output=True, text=True).stdout
    _tmux('kill-session', '-t', 'beats')
    tc.FONT_PX, tc.PAD = 40, 26
    font = tc._font(); cw, ch = tc._cell_size(font)
    img = tc.render_frame(ansi, font, cw, ch)
    a = np.array(img.convert('RGB')); m = a.sum(2) > 46
    ys, xs = np.where(m); p = 26
    img = img.crop((max(0, xs.min() - p), max(0, ys.min() - p),
                    min(img.width, xs.max() + p), min(img.height, ys.max() + p)))
    # place the real pane inside the same editorial window chrome the panels use
    fp = WORK / 'beat_dash.png'
    rp.render_image_panel(fp, inner=img, filename='charon — dashboard',
                          label='THE WORKERS — 12 SHADES RUNNING',
                          caption='Each worker writes a part, renders it, and stitches the result.',
                          folio='02')
    return fp


def make_panel_frame(i: int, panel: dict) -> Path:
    """Render a 'panel' beat from its config (a real artifact in window chrome)."""
    WORK.mkdir(parents=True, exist_ok=True)
    return rp.render_panel(WORK / f'panel{i}.png', **panel)


def still_clip(png: Path, seconds: float, out: Path) -> Path:
    _run([FF, '-y', '-loop', '1', '-t', f'{seconds:.2f}', '-i', str(png),
          '-vf', f'fps={FPS},format=yuv420p', '-c:v', 'libx264', '-crf', '16', str(out)])
    return out


def render_cards() -> dict:
    (WORK / 'editorial.py').write_text((VIDEOS / 'editorial.py').read_text())
    (WORK / 'diagram.py').write_text((VIDEOS / 'diagram.py').read_text())
    (WORK / 'scenes.py').write_text(SCENES)
    scenes = ['VideoIntro', 'Outro'] + [b['name'] for b in BEATS if b['kind'] == 'scene']
    _run([MANIM, '-qh', '--media_dir', str(WORK / 'media'), str(WORK / 'scenes.py'),
          *scenes], cwd=str(WORK))
    q = WORK / 'media' / 'videos' / 'scenes' / '1080p60'
    return {'intro': q / 'VideoIntro.mp4', 'outro': q / 'Outro.mp4',
            **{b['name']: q / f"{b['name']}.mp4" for b in BEATS if b['kind'] == 'scene'}}


def _reencode(src: Path, dst: Path) -> Path:
    _run([FF, '-y', '-i', str(src), '-r', str(FPS), '-vf', f'scale={W}:{H},format=yuv420p',
          '-c:v', 'libx264', '-crf', '18', '-an', str(dst)])
    return dst


def build() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    lines = _script_lines(FEATURE['number'])      # beat narration (the ## NN block)
    cards = render_cards()

    # The intro is voiced with its own introductory line (says what the video is
    # about); the content beats use script lines 1..N; the outro uses the last line.
    intro_vo = hero._mlx_say(FEATURE['intro_line'], WORK / 'vo_intro')
    beat_vos = [hero._mlx_say(lines[i + 1], WORK / f'vo{i}') for i in range(len(BEATS))]
    outro_vo = hero._mlx_say(lines[-1], WORK / 'vo_outro')

    segments = [(cards['intro'], intro_vo)]
    for i, beat in enumerate(BEATS):
        vo = beat_vos[i]
        if beat['kind'] == 'panel':
            frame = make_panel_frame(i, beat['panel'])
            dur = max(5.0, _duration(vo) + 2.6)          # generous tail
            still = still_clip(frame, dur, WORK / f'still{i}.mp4')
            clip = tilt3d.render(still, WORK / f'tilt{i}.mp4', ui_w=1520,
                                 yaw0=beat['yaw'][0], yaw1=beat['yaw'][1])
        else:                                            # animated diagram scene
            clip = cards[beat['name']]
        segments.append((clip, vo))
    segments.append((cards['outro'], outro_vo))

    # Normalize each segment; pad fixed scenes (intro/outro) so they fully contain
    # their narration (otherwise a long line bleeds into the next segment).
    norm_parts = []
    for i, (vid, vo) in enumerate(segments):
        nv = _reencode(vid, WORK / f'seg{i}.mp4')
        need = (_duration(vo) + 1.0) if vo else 0
        if need > _duration(nv):
            padded = WORK / f'seg{i}_pad.mp4'
            _run([FF, '-y', '-i', str(nv), '-vf',
                  f'tpad=stop_mode=clone:stop_duration={need - _duration(nv):.2f}',
                  '-c:v', 'libx264', '-crf', '18', '-an', str(padded)])
            nv = padded
        norm_parts.append(nv)
    silent = WORK / 'silent.mp4'
    silent, starts = transitions.chain(norm_parts, silent, specs=TRANSITION_SPECS, fps=FPS)

    voice_inputs, amix, n = [], [], 0
    for ((_, vo), start) in zip(segments, starts):
        if vo is None:
            continue
        delay = int(max(0, start * 1000) + 400)
        voice_inputs += ['-i', str(vo)]
        amix.append(f'[{n}:a]adelay={delay}|{delay}[a{n}]')
        n += 1
    voice = WORK / 'voice.wav'
    _run([FF, '-y', *voice_inputs, '-filter_complex',
          ';'.join(amix) + ';' + ''.join(f'[a{i}]' for i in range(n))
          + f'amix=inputs={n}:duration=longest:normalize=0[v]',
          '-map', '[v]', '-ar', '24000', '-ac', '1', str(voice)])

    # Pad the video so it fully contains the narration (fixes the cut-off ending),
    # plus a 1.2s tail to let the last line and music breathe out.
    vid_dur, voc_dur = _duration(silent), _duration(voice)
    total = max(vid_dur, voc_dur) + 1.2
    bed = hero.music_bed(total + 0.5, WORK / 'music.mp3')
    graded = WORK / 'graded.mp4'
    _run([FF, '-y', '-i', str(silent), '-vf',
          f'{GRADE_VF},tpad=stop_mode=clone:stop_duration={total - vid_dur:.2f}',
          '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', '-an', str(graded)])
    final = OUT / f"charon-feature-{FEATURE['number']}-{FEATURE['title'].lower().replace(' ', '-')}.mp4"
    _run([FF, '-y', '-i', str(graded), '-i', str(voice), '-i', str(bed),
          '-filter_complex',
          '[1:a]apad,asplit=2[voice][vkey];[2:a]volume=0.5[bed];'
          '[bed][vkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=320[duckm];'
          '[voice][duckm]amix=inputs=2:duration=first,'
          'loudnorm=I=-18:TP=-2:LRA=11,alimiter=limit=0.79:level=disabled,'
          'aresample=48000[a]',                      # 48k — loudnorm emits 96k, which QuickTime can't play
          '-map', '0:v', '-map', '[a]', '-r', str(FPS), '-vsync', 'cfr',
          '-c:v', 'libx264', '-profile:v', 'high', '-crf', '19', '-pix_fmt', 'yuv420p', '-g', str(FPS * 2),
          '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '256k',
          '-movflags', '+faststart', '-shortest', str(final)])
    return final


def _script_lines(number: str) -> list[str]:
    import re
    md = (VIDEOS / 'scripts.md').read_text(encoding='utf-8')
    out, grab = [], False
    for ln in md.splitlines():
        if ln.startswith('## ') and f'{number} —' in ln:
            grab = True; continue
        if ln.startswith('## ') and grab:
            break
        if grab:
            m = re.search(r'·\s*\*(.+?)\*\s*$', ln)
            if m:
                out.append(m.group(1).strip())
    return out


if __name__ == '__main__':
    print('built ->', build())
