"""Render real text output (a manifest, a judge result) as a large editorial
code window that fills the frame — the visual judge wants the feature's RESULT
on screen, not the command. Used for beats whose payoff is a file/artifact.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG = (11, 10, 9)
PANEL = (16, 19, 25)
EDGE = (38, 46, 56)
TITLEBAR = (22, 27, 34)
PAPER = (236, 231, 222)
MUTE = (133, 124, 112)
GUTTER = (74, 82, 94)
ACCENT = (224, 133, 42)      # amber — keys / numbers
CYAN = (34, 211, 238)        # cyan — json keys / structure
GREEN = (124, 227, 139)      # green — string values
DOT = ((255, 95, 87), (254, 188, 46), (40, 200, 64))

MENLO = '/System/Library/Fonts/Menlo.ttc'
HELV = '/System/Library/Fonts/HelveticaNeue.ttc'


def _trk(d, xy, t, f, fill, sp):
    x, y = xy
    for c in t:
        d.text((x, y), c, font=f, fill=fill)
        x += d.textlength(c, font=f) + sp
    return x


def _color_yaml(line: str):
    """Yield (text, color) spans for a YAML line."""
    m = re.match(r'^(\s*)([\w\-]+)(:)(.*)$', line)
    if m:
        ind, key, colon, rest = m.groups()
        spans = [(ind, PAPER), (key, ACCENT), (colon, MUTE)]
        spans += _color_value(rest)
        return spans
    m = re.match(r'^(\s*-\s*)(.*)$', line)
    if m:
        return [(m.group(1), MUTE)] + _color_value(m.group(2))
    return [(line, PAPER)]


def _color_value(s: str):
    out = []
    for tok in re.split(r'(\".*?\"|\[|\]|,)', s):
        if not tok:
            continue
        if tok.startswith('"'):
            out.append((tok, GREEN))
        elif tok in '[],':
            out.append((tok, MUTE))
        elif re.fullmatch(r'\s*\d+\s*', tok):
            out.append((tok, ACCENT))
        else:
            out.append((tok, PAPER))
    return out


def _color_json(line: str):
    out = []
    for tok in re.split(r'(\".*?\"\s*:|\".*?\"|[\d.]+|[{}\[\],])', line):
        if not tok:
            continue
        if tok.rstrip().endswith(':') and tok.lstrip().startswith('"'):
            out.append((tok, CYAN))
        elif tok.startswith('"'):
            out.append((tok, GREEN))
        elif re.fullmatch(r'[\d.]+', tok):
            out.append((tok, ACCENT))
        elif tok in '{}[],':
            out.append((tok, MUTE))
        else:
            out.append((tok, PAPER))
    return out


def _window_chrome(d, filename, label, caption, folio):
    helv = lambda s: ImageFont.truetype(HELV, s)
    monos = ImageFont.truetype(MENLO, 22)
    px0, px1, py0, py1 = 150, W - 150, 150, H - 150
    bar = 56
    _trk(d, (px0, py0 - 48), label, helv(17), ACCENT, 5)
    flw = sum(d.textlength(c, font=helv(20)) + 3 for c in folio)
    _trk(d, (px1 - flw, py0 - 52), folio, helv(20), MUTE, 3)
    _trk(d, (px0, py1 + 22), caption, helv(17), MUTE, 2)
    d.rounded_rectangle([px0, py0, px1, py1], radius=18, fill=PANEL, outline=EDGE, width=2)
    d.rounded_rectangle([px0, py0, px1, py0 + bar], radius=18, fill=TITLEBAR)
    d.rectangle([px0, py0 + bar - 18, px1, py0 + bar], fill=TITLEBAR)
    for i, c in enumerate(DOT):
        d.ellipse([px0 + 26 + i * 30, py0 + bar // 2 - 8, px0 + 42 + i * 30, py0 + bar // 2 + 8], fill=c)
    fnw = d.textlength(filename, font=monos)
    d.text(((W - fnw) / 2, py0 + bar // 2 - 14), filename, font=monos, fill=MUTE)
    return px0, px1, py0, py1, bar


def render_panel(out: Path, *, content: str, filename: str, label: str, caption: str,
                 syntax: str = 'yaml', folio: str = '01', highlight: int = -1) -> Path:
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    mono = ImageFont.truetype(MENLO, 30)
    monos = ImageFont.truetype(MENLO, 22)
    px0, px1, py0, py1, bar = _window_chrome(d, filename, label, caption, folio)

    cx, cy = px0 + 40, py0 + bar + 28
    lh = 44
    colorize = _color_json if syntax == 'json' else _color_yaml
    for i, line in enumerate(content.splitlines()):
        y = cy + i * lh
        if y > py1 - 50:
            break
        if i == highlight:   # spotlight the key line (judge: stronger hierarchy)
            d.rounded_rectangle([px0 + 16, y - 8, px1 - 16, y + 40], radius=8,
                                fill=(28, 32, 24))
            d.rectangle([px0 + 16, y - 8, px0 + 22, y + 40], fill=ACCENT)
        d.text((cx, y), f'{i+1:>2}', font=monos, fill=GUTTER)
        x = cx + 70
        for tok, col in colorize(line):
            d.text((x, y), tok, font=mono, fill=col)
            x += d.textlength(tok, font=mono)
    img.save(out)
    return out


def render_image_panel(out: Path, *, inner: Image.Image, filename: str, label: str,
                       caption: str, folio: str = '01') -> Path:
    """Place a captured UI image inside the editorial window chrome."""
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    px0, px1, py0, py1, bar = _window_chrome(d, filename, label, caption, folio)
    cw, ch_ = (px1 - px0 - 36), (py1 - (py0 + bar) - 28)
    s = min(cw / inner.width, ch_ / inner.height)
    scaled = inner.resize((int(inner.width * s), int(inner.height * s)), Image.LANCZOS)
    ox = px0 + 18 + (cw - scaled.width) // 2
    oy = py0 + bar + 14 + (ch_ - scaled.height) // 2
    img.paste(scaled, (ox, oy))
    img.save(out)
    return out


if __name__ == '__main__':
    import sys
    render_panel(Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/panel.png'),
                 content='reel:\n  app: "Charon"\n  order: [memory, shades, video]\nfeatures:\n  - slug: memory\n    title: "Memory"\n    seconds: 15',
                 filename='features.yaml', label='THE DIRECTOR — PROPOSED MANIFEST',
                 caption='Reads the project and proposes the features to show.', syntax='yaml')
    print('saved')
