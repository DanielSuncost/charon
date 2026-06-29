"""Browser capture — record a deployed web app, the way termcap records the TUI.

Charon drives a real Chromium session with Playwright (logging in via a saved
session), runs a per-feature action script, and records video. The result drops
into the exact same production pipeline (editorial frame → tilt → narration →
diagrams → music → transitions → judge) as the terminal capture, so the whole
system works on any website.

    # 1. one-time: log in by hand, save the authenticated session
    python videos/webcap.py login https://app.example.com auth/example.json
    # 2. record a feature flow (reusing that session)
    python videos/webcap.py record https://app.example.com/dashboard out.mp4 auth/example.json

Action scripts (for the pipeline) are lists of dicts:
    {'goto': url} {'click': sel} {'fill': [sel, text]} {'press': key}
    {'hover': sel} {'scroll': px} {'wait': seconds} {'waitfor': sel}
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

FF = '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'
SIZE = (1600, 900)
FPS = 30


def save_login(url: str, state_path: Path, *, size=SIZE, timeout_s: int = 240) -> Path:
    """Open a HEADED browser, let the user log in by hand, then save the session.

    Waits until the user presses Enter in the terminal (or the timeout), then
    persists cookies + localStorage to state_path for reuse by record().
    """
    from playwright.sync_api import sync_playwright
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={'width': size[0], 'height': size[1]})
        page = ctx.new_page()
        page.goto(url)
        print(f'\n>>> Log into {url} in the browser window, then press Enter here to save the session...')
        try:
            input()
        except EOFError:
            time.sleep(timeout_s)
        ctx.storage_state(path=str(state_path))
        browser.close()
    print(f'saved session -> {state_path}')
    return state_path


def _run_action(page, step: dict):
    if 'goto' in step:
        page.goto(step['goto'], wait_until='networkidle')
    elif 'click' in step:
        page.click(step['click'], timeout=15000)
    elif 'fill' in step:
        sel, txt = step['fill']
        page.fill(sel, txt, timeout=15000)
    elif 'press' in step:
        page.keyboard.press(step['press'])
    elif 'hover' in step:
        page.hover(step['hover'], timeout=15000)
    elif 'scroll' in step:
        page.mouse.wheel(0, step['scroll'])
    elif 'waitfor' in step:
        page.wait_for_selector(step['waitfor'], timeout=20000)
    elif 'wait' in step:
        time.sleep(step['wait'])


def record(url: str, out_mp4: Path, *, actions: list[dict] | None = None,
           state_path: Path | None = None, size=SIZE, settle: float = 1.5) -> Path:
    """Record a browser session driving `url` through `actions` -> mp4.

    Uses a saved auth session if state_path is given. The webm Playwright writes
    is transcoded to a pipeline-friendly mp4 (h264/yuv420p/30fps).
    """
    from playwright.sync_api import sync_playwright
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    vdir = out_mp4.parent / '_webcap_raw'
    vdir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kw = dict(viewport={'width': size[0], 'height': size[1]},
                      record_video_dir=str(vdir),
                      record_video_size={'width': size[0], 'height': size[1]})
        if state_path and Path(state_path).exists():
            ctx_kw['storage_state'] = str(state_path)
        ctx = browser.new_context(**ctx_kw)
        page = ctx.new_page()
        page.goto(url, wait_until='networkidle')
        time.sleep(settle)
        for step in (actions or []):
            _run_action(page, step)
            time.sleep(0.4)
        time.sleep(settle)
        webm = page.video.path()
        ctx.close()           # finalizes the recording
        browser.close()
    subprocess.run([FF, '-y', '-i', str(webm), '-vf', f'fps={FPS},format=yuv420p',
                    '-c:v', 'libx264', '-crf', '18', str(out_mp4)], check=True, capture_output=True)
    return out_mp4


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'login':
        save_login(sys.argv[2], Path(sys.argv[3]))
    elif cmd == 'record':
        state = Path(sys.argv[4]) if len(sys.argv) > 4 else None
        print('recorded ->', record(sys.argv[2], Path(sys.argv[3]), state_path=state))
    else:
        print(__doc__)
