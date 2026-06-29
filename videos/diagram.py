"""Diagram kit — elite, animated, editorial-quality diagrams (3Blue1Brown rigor
× IDEA/Hermès restraint), drawn in manim so they animate in-engine and match the
brand exactly: warm near-black ground, paper type, one amber accent, cyan
structure, soft glow + transparency, precise eased motion.

Scene builders (`swarm_scene`, `loop_scene`, `pipeline_scene`) draw a complete
beat — editorial framing + the animated diagram — timed to a narration line.
Import alongside editorial: `from editorial import *` then `from diagram import *`.

A D2 path (`render_d2`) also exists for static auto-laid-out architecture diagrams.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from manim import *

from editorial import BG, PAPER, MUTE, FAINT, ACCENT, MONO, SANS, margins, hairline, label

NODE_FILL = "#131210"      # panel-dark
CYAN = "#5BC8D8"           # structural / workers (muted, not neon)
GREEN = "#8FBF8A"          # output / success


def _glow(mob, color, layers=10, spread=0.5, max_opacity=0.13):
    """Soft radial bloom behind a shape (transparency-layered, no glyph artifacts)."""
    halo = VGroup()
    for i in range(layers, 0, -1):
        c = mob.copy().set_stroke(width=0).set_fill(color, opacity=max_opacity * (1 - i / layers))
        c.scale(1 + spread * (i / layers))
        halo.add(c)
    return halo.move_to(mob)


def dnode(text: str, *, w=2.7, h=1.0, accent=PAPER, sub: str = "", fs=26) -> VGroup:
    """A refined node: thin border, panel fill, mono label, optional tracked sub-label."""
    box = RoundedRectangle(corner_radius=0.12, width=w, height=h,
                           stroke_color=accent, stroke_width=1.6).set_fill(NODE_FILL, opacity=1.0)
    lab = Text(text, font=MONO, color=PAPER, font_size=fs).move_to(box)
    g = VGroup(box, lab)
    if sub:
        g.add(label(sub, 3, MUTE, 13).next_to(lab, DOWN, buff=0.1))
    g.box = box
    g.accent = accent
    return g


def dedge(a, b, *, a_side=RIGHT, b_side=LEFT, color=FAINT, width=1.8):
    s = (a.box if hasattr(a, 'box') else a).get_edge_center(a_side)
    e = (b.box if hasattr(b, 'box') else b).get_edge_center(b_side)
    return Line(s, e, color=color, stroke_width=width)


def _frame(scene: Scene, *, kicker: str, caption: str, folio: str):
    """Editorial chrome shared by every diagram beat (matches the cards)."""
    L, R, T, B = margins()
    top, bot = hairline(L, R, T), hairline(L, R, B)
    mast = label(kicker, 6, ACCENT, 14).next_to(top, UP, buff=0.14).align_to(top, LEFT)
    fol = label(folio, 4, MUTE, 13).next_to(top, UP, buff=0.16).align_to(top, RIGHT)
    cap = label(caption, 3, MUTE, 14).next_to(bot, UP, buff=0.18).align_to(bot, LEFT)
    scene.play(LaggedStart(Create(top), Create(bot), FadeIn(mast), FadeIn(fol), FadeIn(cap),
                           lag_ratio=0.15, run_time=0.9))


def pulse_along(scene: Scene, edges, color, run_time=0.9, lag=0.1, radius=0.06):
    """A bead of light travels each edge — 'work dispatched'."""
    dots = [Dot(color=color, radius=radius).move_to(e.get_start()) for e in edges]
    glows = [_glow(d, color, layers=5, spread=1.4, max_opacity=0.5) for d in dots]
    grp = [VGroup(g, d) for g, d in zip(glows, dots)]
    paths = [MoveAlongPath(grp[i], edges[i]) for i in range(len(edges))]
    scene.play(LaggedStart(*paths, lag_ratio=lag, run_time=run_time), rate_func=smooth)
    scene.play(*[FadeOut(x) for x in grp], run_time=0.3)


# ── Beat: the swarm (coordinator fans out to shades, work flows to a film) ──
def swarm_scene(scene: Scene, *, children, kicker="THE WORKERS",
                caption="ONE COORDINATOR · MANY SHADES · ONE FILM", folio="02 / 09",
                hold=1.6):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)

    coord = dnode("coordinator", w=3.0, h=1.25, accent=ACCENT, sub="1 AGENT", fs=28).move_to([-4.6, 0, 0])
    shades = VGroup(*[dnode(t, w=2.9, h=0.82, accent=CYAN, fs=22) for t in children])
    shades.arrange(DOWN, buff=0.34).move_to([0.2, 0, 0])
    film = dnode("final.mp4", w=2.5, h=1.0, accent=GREEN, fs=24).move_to([5.0, 0, 0])

    # coordinator appears with glow
    cg = _glow(coord.box, ACCENT)
    scene.play(FadeIn(cg), GrowFromCenter(coord), run_time=0.7, rate_func=smooth)
    # fan-out edges draw, shades grow, in a staggered, eased cascade
    out_edges = [dedge(coord, s, color=MUTE) for s in shades]
    scene.play(LaggedStart(*[Create(e) for e in out_edges], lag_ratio=0.12, run_time=0.8), rate_func=smooth)
    scene.play(LaggedStart(*[GrowFromCenter(s) for s in shades], lag_ratio=0.12, run_time=0.8))
    pulse_along(scene, out_edges, CYAN)
    # shades converge to the film
    in_edges = [dedge(s, film, color=MUTE) for s in shades]
    fg = _glow(film.box, GREEN)
    scene.play(LaggedStart(*[Create(e) for e in in_edges], lag_ratio=0.1, run_time=0.7), rate_func=smooth)
    scene.play(FadeIn(fg), GrowFromCenter(film), run_time=0.6)
    pulse_along(scene, in_edges, GREEN, lag=0.06)
    scene.wait(hold)


# ── Beat: the judge loop (render -> judge -> fixes -> render, a cycle) ──
def loop_scene(scene: Scene, *, kicker="THE JUDGE",
               caption="RENDER · SCORE · FIX · REPEAT", folio="03 / 09", hold=1.6):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    render = dnode("render", w=2.6, h=1.0, accent=CYAN, fs=26).move_to([-3.0, 1.1, 0])
    judge = dnode("judge", w=2.6, h=1.0, accent=ACCENT, sub="VISION SCORE", fs=26).move_to([3.0, 1.1, 0])
    fix = dnode("apply fix", w=2.6, h=1.0, accent=GREEN, fs=24).move_to([0, -1.6, 0])
    for n, acc in [(render, CYAN), (judge, ACCENT), (fix, GREEN)]:
        scene.play(FadeIn(_glow(n.box, acc)), GrowFromCenter(n), run_time=0.5)
    e1 = dedge(render, judge, color=MUTE)
    e2 = Line(judge.box.get_bottom(), fix.box.get_right(), color=MUTE, stroke_width=1.8)
    e3 = Line(fix.box.get_left(), render.box.get_bottom(), color=MUTE, stroke_width=1.8)
    scene.play(LaggedStart(Create(e1), Create(e2), Create(e3), lag_ratio=0.2, run_time=1.0), rate_func=smooth)
    for _ in range(2):
        pulse_along(scene, [e1, e2, e3], ACCENT, run_time=1.2, lag=0.0)
    scene.wait(hold)


# ── D2 (static, auto-laid-out architecture diagrams) ─────────────────
def render_d2(spec: str, out_png: Path, *, pad: int = 40, theme: int = 200) -> Path:
    if shutil.which('d2') is None:
        raise RuntimeError('d2 not installed. Run: brew install d2')
    d2f = out_png.with_suffix('.d2')
    d2f.write_text('vars: { d2-config: { theme-id: %d; pad: %d } }\n%s' % (theme, pad, spec), encoding='utf-8')
    subprocess.run(['d2', '--scale', '3', str(d2f), str(out_png)], check=True, capture_output=True)
    return out_png
