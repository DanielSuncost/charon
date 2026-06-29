"""3D screen tilt/pan for UI footage — the Apple-style floating-screen effect.

Treats a UI capture as a plane in 3D and renders it with an animated perspective
(slow Y-axis rotation + drift + float) plus a soft drop shadow, composited on the
editorial background. Frame-by-frame with PIL for full control.

    python videos/tilt3d.py <in.mp4> <out.mp4>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

W, H, FPS = 1920, 1080, 30
BG = (11, 10, 9)
FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'


def crop_to_content(in_mp4: Path, out_mp4: Path, *, pad_frac: float = 0.015) -> Path:
    """Crop a UI capture to the union bounding box of its actual content.

    Kills the dead near-black space around the terminal so the UI fills the
    frame after compositing (the visual judge's top fix). Samples frames across
    the clip and unions their non-background bounding boxes.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    subprocess.run([FF, '-y', '-i', str(in_mp4), '-vf', 'fps=2,scale=960:-1',
                    str(tmp / 's%03d.png')], check=True, capture_output=True)
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    sw = sh = None
    for p in sorted(tmp.glob('s*.png')):
        a = np.asarray(Image.open(p).convert('RGB'))
        sh, sw = a.shape[:2]
        m = a.sum(2) > 48
        ys, xs = np.where(m)
        if len(xs):
            x0, x1 = min(x0, xs.min()), max(x1, xs.max())
            y0, y1 = min(y0, ys.min()), max(y1, ys.max())
    # get true source size
    probe = subprocess.run([FF.replace('ffmpeg', 'ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(in_mp4)],
                           capture_output=True, text=True)
    W0, H0 = (int(x) for x in probe.stdout.strip().split(','))
    fx, fy = W0 / sw, H0 / sh
    px, py = int(W0 * pad_frac), int(H0 * pad_frac)
    cx0 = max(0, int(x0 * fx) - px)
    cy0 = max(0, int(y0 * fy) - py)
    cw = min(W0 - cx0, int((x1 - x0) * fx) + 2 * px)
    ch = min(H0 - cy0, int((y1 - y0) * fy) + 2 * py)
    # even dims for h264
    cw -= cw % 2
    ch -= ch % 2
    subprocess.run([FF, '-y', '-i', str(in_mp4), '-vf', f'crop={cw}:{ch}:{cx0}:{cy0}',
                    '-c:v', 'libx264', '-crf', '16', str(out_mp4)], check=True, capture_output=True)
    return out_mp4


def _coeffs(target, source):
    M = []
    for (xt, yt), (xs, ys) in zip(target, source):
        M.append([xs, ys, 1, 0, 0, 0, -xt * xs, -xt * ys])
        M.append([0, 0, 0, xs, ys, 1, -yt * xs, -yt * ys])
    A = np.array(M, dtype=float)
    B = np.array(target).reshape(8)
    return np.linalg.solve(A.T @ A, A.T @ B)


def _smooth(t: float) -> float:
    return t * t * (3 - 2 * t)  # smoothstep


def _quad(uw, uh, yaw, drift_x, drift_y, scale):
    """Destination quad for a Y-axis yaw (radians) at given drift/scale.

    Gentle foreshortening keeps the window large while still reading as 3D.
    """
    cx, cy = W / 2 + drift_x, H / 2 + drift_y
    hw, hh = uw * scale / 2, uh * scale / 2
    fore = np.sin(yaw)
    far = 1.0 - 0.45 * abs(fore)     # strong foreshortening -> obvious 3D
    shift = 300 * fore               # strong horizontal recession
    if fore >= 0:   # right edge far
        lx, rx = -hw, hw - shift
        ltop, lbot = cy - hh, cy + hh
        rtop, rbot = cy - hh * far, cy + hh * far
    else:           # left edge far
        lx, rx = -hw - shift, hw
        ltop, lbot = cy - hh * far, cy + hh * far
        rtop, rbot = cy - hh, cy + hh
    return [(cx + lx, ltop), (cx + rx, rtop), (cx + rx, rbot), (cx + lx, lbot)]


def render(in_mp4: Path, out_mp4: Path, *, ui_w: int = 1780,
           yaw0: float = 0.11, yaw1: float = -0.07) -> Path:
    """Render in_mp4 as a floating tilted screen panning yaw0 -> yaw1."""
    work = out_mp4.parent / '_tilt_frames'
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob('*.png'):
        f.unlink()

    # explode source to frames
    src_dir = work / 'src'
    src_dir.mkdir(exist_ok=True)
    subprocess.run([FF, '-y', '-i', str(in_mp4), '-vf', f'fps={FPS}',
                    str(src_dir / 'f%04d.png')], check=True, capture_output=True)
    frames = sorted(src_dir.glob('f*.png'))
    n = len(frames)

    for i, fp in enumerate(frames):
        t = _smooth(i / max(1, n - 1))
        yaw = yaw0 + (yaw1 - yaw0) * t
        drift_x = (1 - 2 * t) * 60          # clearly visible horizontal pan
        drift_y = np.sin(t * np.pi) * -10
        scale = 1.0 + 0.03 * t              # slow dolly-in (parallax)

        ui = Image.open(fp).convert('RGB')
        w, h = ui.size
        s = ui_w / w
        uw, uh = int(w * s), int(h * s)
        ui = ui.resize((uw, uh), Image.LANCZOS)
        src = [(0, 0), (uw, 0), (uw, uh), (0, uh)]
        tgt = _quad(uw, uh, yaw, drift_x, drift_y, scale)
        co = _coeffs(src, tgt)

        canvas = Image.new('RGB', (W, H), BG)
        warped = ui.transform((W, H), Image.PERSPECTIVE, co, Image.BICUBIC)
        mask = Image.new('L', (uw, uh), 255).transform((W, H), Image.PERSPECTIVE, co)
        # soft drop shadow offset down-right
        sh = mask.filter(ImageFilter.GaussianBlur(26)).point(lambda a: int(a * 0.55))
        shx, shy = 22, 34
        sh_off = Image.new('L', (W, H), 0)
        sh_off.paste(sh, (shx, shy))
        canvas.paste(Image.new('RGB', (W, H), (0, 0, 0)), (0, 0), sh_off)
        canvas.paste(warped, (0, 0), mask)
        canvas.save(work / f'o{i:04d}.png')

    subprocess.run([FF, '-y', '-framerate', str(FPS), '-i', str(work / 'o%04d.png'),
                    '-vf', 'format=yuv420p', '-c:v', 'libx264', '-crf', '18', str(out_mp4)],
                   check=True, capture_output=True)
    return out_mp4


if __name__ == '__main__':
    render(Path(sys.argv[1]), Path(sys.argv[2]))
    print('tilted ->', sys.argv[2])
