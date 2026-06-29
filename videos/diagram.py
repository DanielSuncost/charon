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


RED = "#E8746A"


# ── Beat: the scope contract (a shade can only touch its given files) ──
def contract_scene(scene: Scene, *, kicker="THE CONTRACT",
                   caption="A SHADE TOUCHES ONLY WHAT IT WAS GIVEN", folio="02 / 09", hold=1.6):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    shade = dnode("shade", w=2.6, h=1.05, accent=CYAN, sub="SCOPED", fs=26).move_to([-4.4, 0, 0])
    scene.play(FadeIn(_glow(shade.box, CYAN)), GrowFromCenter(shade), run_time=0.6)
    # allowed files inside a boundary
    allowed = VGroup(*[dnode(t, w=2.6, h=0.7, accent=GREEN, fs=20) for t in ["src/render.py", "out/clip.mp4"]])
    allowed.arrange(DOWN, buff=0.4).move_to([1.4, 0.0, 0])
    boundary = RoundedRectangle(corner_radius=0.2, width=3.4, height=2.6,
                                stroke_color=GREEN, stroke_width=1.4).set_fill(GREEN, opacity=0.04).move_to(allowed)
    blab = label("IN SCOPE", 4, GREEN, 13).next_to(boundary, UP, buff=0.14)
    denied = dnode("system files", w=2.8, h=0.8, accent=RED, fs=20).move_to([5.0, -1.6, 0])
    scene.play(Create(boundary), FadeIn(blab), run_time=0.6)
    scene.play(LaggedStart(*[GrowFromCenter(a) for a in allowed], lag_ratio=0.2, run_time=0.7))
    ok = [dedge(shade, a, color=GREEN) for a in allowed]
    scene.play(LaggedStart(*[Create(e) for e in ok], lag_ratio=0.2, run_time=0.6))
    pulse_along(scene, ok, GREEN)
    # blocked write to a system file: edge drawn, then an X stops it
    scene.play(GrowFromCenter(denied), run_time=0.4)
    block = DashedVMobject(dedge(shade, denied, color=RED), num_dashes=16)
    cross = VGroup(Line(UL, DR, color=RED, stroke_width=5), Line(UR, DL, color=RED, stroke_width=5)).scale(0.22)
    cross.move_to(block.get_center())
    scene.play(Create(block), run_time=0.5)
    scene.play(GrowFromCenter(cross), run_time=0.3)
    scene.wait(hold)


# ── Beat: the budget (tokens / time / tries deplete, then it stops) ──
def budget_scene(scene: Scene, *, kicker="THE BUDGET",
                 caption="TOKENS · TIME · ATTEMPTS — THEN IT STOPS", folio="02 / 09", hold=1.4):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    shade = dnode("shade", w=2.6, h=1.05, accent=CYAN, fs=26).move_to([-4.4, 0, 0])
    scene.play(FadeIn(_glow(shade.box, CYAN)), GrowFromCenter(shade), run_time=0.6)
    meters, fills, names, fracs = VGroup(), [], ["tokens", "time", "attempts"], [0.18, 0.32, 0.1]
    for k, (nm, fr) in enumerate(zip(names, fracs)):
        y = 1.1 - k * 1.1
        track = RoundedRectangle(corner_radius=0.1, width=5.0, height=0.5,
                                 stroke_color=FAINT, stroke_width=1.4).set_fill(NODE_FILL, opacity=1).move_to([2.0, y, 0])
        lab = label(nm, 4, MUTE, 14).next_to(track, LEFT, buff=0.3)
        fill = RoundedRectangle(corner_radius=0.1, width=4.8, height=0.36, stroke_width=0).set_fill(ACCENT, opacity=0.9)
        fill.align_to(track, LEFT).shift(RIGHT * 0.1).set_y(y)
        meters.add(track, lab); fills.append((fill, fr))
    scene.play(LaggedStart(*[FadeIn(m) for m in meters], lag_ratio=0.1, run_time=0.7))
    scene.play(*[FadeIn(f) for f, _ in fills], run_time=0.3)
    # deplete each bar toward its remaining fraction
    anims = []
    for fill, fr in fills:
        target = fill.copy().stretch_to_fit_width(4.8 * fr).align_to(fill, LEFT)
        anims.append(Transform(fill, target))
    scene.play(LaggedStart(*anims, lag_ratio=0.15, run_time=1.6), rate_func=smooth)
    stop = label("BUDGET SPENT — STOP", 4, RED, 15).next_to(shade, DOWN, buff=0.5)
    scene.play(FadeIn(stop), shade.box.animate.set_stroke(RED), run_time=0.5)
    scene.wait(hold)


def _fit(grp, max_w=12.6, max_h=5.6):
    if grp.width > max_w:
        grp.scale(max_w / grp.width)
    if grp.height > max_h:
        grp.scale(max_h / grp.height)
    return grp


# ── Beat: a left-to-right pipeline (stages light up, a bead flows through) ──
def pipeline_scene(scene: Scene, *, stages, kicker="THE PIPELINE", caption="",
                   folio="01 / 09", hold=1.5):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    cyc = [CYAN, ACCENT, CYAN, GREEN]
    nodes = VGroup(*[dnode(s, w=2.7, h=0.95, accent=cyc[i % len(cyc)], fs=21)
                     for i, s in enumerate(stages)])
    nodes.arrange(RIGHT, buff=0.85)
    _fit(nodes).move_to([0, -0.1, 0])
    edges = [dedge(nodes[i], nodes[i + 1], color=MUTE) for i in range(len(stages) - 1)]
    scene.play(FadeIn(_glow(nodes[0].box, nodes[0].accent)), GrowFromCenter(nodes[0]), run_time=0.5)
    for i in range(len(stages) - 1):
        scene.play(Create(edges[i]), run_time=0.32)
        scene.play(FadeIn(_glow(nodes[i + 1].box, nodes[i + 1].accent)),
                   GrowFromCenter(nodes[i + 1]), run_time=0.42)
    pulse_along(scene, edges, ACCENT, run_time=1.5, lag=0.28)
    scene.wait(hold)


# ── Beat: a ranked result list (bars grow, the top match is held in amber) ──
def ranked_scene(scene: Scene, *, rows, kicker="THE RANKING", caption="",
                 folio="01 / 09", hold=1.5, highlight=0):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    TW, dy, top_y, TX = 5.8, 0.92, 1.55, 2.9
    chrome, bars = VGroup(), []
    for i, (txt, frac) in enumerate(rows):
        y = top_y - i * dy
        on = (i == highlight)
        acc = ACCENT if on else CYAN
        lab = label(txt.upper(), 2, PAPER if on else MUTE, 15)
        if lab.width > 5.2:
            lab.scale(5.2 / lab.width)
        lab.next_to([-6.3, y, 0], RIGHT, buff=0)
        track = RoundedRectangle(corner_radius=0.08, width=TW, height=0.46, stroke_color=FAINT,
                                 stroke_width=1.2).set_fill(NODE_FILL, 1).move_to([TX, y, 0])
        fill = Rectangle(width=0.04, height=0.34, stroke_width=0).set_fill(acc, 0.95 if on else 0.55)
        fill.next_to(track.get_left(), RIGHT, buff=0.08).set_y(y)
        chrome.add(lab, track); bars.append((fill, frac, track, on))
    scene.play(LaggedStart(*[FadeIn(m) for m in chrome], lag_ratio=0.08, run_time=0.8))
    grows = []
    for fill, frac, track, on in bars:
        scene.add(fill)
        tgt = fill.copy().stretch_to_fit_width(max(0.06, (TW - 0.16) * frac))
        tgt.next_to(track.get_left(), RIGHT, buff=0.08).set_y(fill.get_y())
        grows.append(Transform(fill, tgt))
    scene.play(LaggedStart(*grows, lag_ratio=0.12, run_time=1.3), rate_func=smooth)
    htrack = bars[highlight][2]
    scene.play(FadeIn(_glow(htrack, ACCENT, layers=6, spread=0.28, max_opacity=0.1)), run_time=0.4)
    scene.wait(hold)


# ── Beat: everything stays on the machine (a boundary holds the whole flow) ──
def local_scene(scene: Scene, *, kicker="ON YOUR MACHINE",
                caption="A SEARCH IS ABOUT TEN MILLISECONDS, AND LOCAL", folio="01 / 09", hold=1.5):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    query = dnode("query", w=2.4, h=0.95, accent=ACCENT, fs=24).move_to([-4.6, 0, 0])
    store = dnode("local db", w=2.8, h=1.1, accent=CYAN, sub="SQLITE", fs=24).move_to([0, 0, 0])
    result = dnode("ranked recall", w=2.9, h=0.95, accent=GREEN, fs=21).move_to([4.7, 0, 0])
    boundary = RoundedRectangle(corner_radius=0.22, width=12.6, height=3.2, stroke_color=FAINT,
                                stroke_width=1.3).set_fill(CYAN, opacity=0.025).move_to([0, 0, 0])
    blab = label("YOUR MACHINE — NEVER LEAVES", 4, MUTE, 13).next_to(boundary, UP, buff=0.16).align_to(boundary, LEFT)
    scene.play(Create(boundary), FadeIn(blab), run_time=0.6)
    scene.play(FadeIn(_glow(query.box, ACCENT)), GrowFromCenter(query), run_time=0.45)
    e1 = dedge(query, store, color=MUTE)
    scene.play(Create(e1), FadeIn(_glow(store.box, CYAN)), GrowFromCenter(store), run_time=0.5)
    e2 = dedge(store, result, color=MUTE)
    scene.play(Create(e2), FadeIn(_glow(result.box, GREEN)), GrowFromCenter(result), run_time=0.5)
    pulse_along(scene, [e1, e2], CYAN, run_time=1.1, lag=0.2)
    stamp = label("≈ 10 MS", 3, ACCENT, 16).move_to([2.35, 1.0, 0])
    scene.play(FadeIn(stamp), run_time=0.4)
    scene.wait(hold)


# ── Beat: one source fans out to many targets (no convergence) ──
def fanout_scene(scene: Scene, *, source, targets, kicker="ACROSS PROJECTS", caption="",
                 folio="01 / 09", hold=1.5, src_accent=ACCENT, tgt_accent=CYAN):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    src = dnode(source, w=3.2, h=1.2, accent=src_accent, fs=22).move_to([-4.6, 0, 0])
    tg = VGroup(*[dnode(t, w=3.2, h=0.85, accent=tgt_accent, fs=21) for t in targets])
    tg.arrange(DOWN, buff=0.45).move_to([2.6, 0, 0])
    scene.play(FadeIn(_glow(src.box, src_accent)), GrowFromCenter(src), run_time=0.6)
    edges = [dedge(src, t, color=MUTE) for t in tg]
    scene.play(LaggedStart(*[Create(e) for e in edges], lag_ratio=0.12, run_time=0.7), rate_func=smooth)
    scene.play(LaggedStart(*[GrowFromCenter(t) for t in tg], lag_ratio=0.12, run_time=0.7))
    pulse_along(scene, edges, tgt_accent)
    scene.wait(hold)


# ── Beat: a score branches to keep (better) or roll back (worse) ──
def branch_scene(scene: Scene, *, kicker="KEEP OR ROLL BACK",
                 caption="BETTER SCORE KEEPS · WORSE RESTORES THE SNAPSHOT", folio="03 / 09", hold=1.5):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    score = dnode("new score", w=2.8, h=1.05, accent=ACCENT, fs=24).move_to([-4.4, 0, 0])
    keep = dnode("keep change", w=3.0, h=0.95, accent=GREEN, sub="BETTER", fs=22).move_to([3.4, 1.45, 0])
    roll = dnode("restore snapshot", w=3.6, h=0.95, accent=RED, sub="WORSE", fs=20).move_to([3.6, -1.45, 0])
    scene.play(FadeIn(_glow(score.box, ACCENT)), GrowFromCenter(score), run_time=0.6)
    ek = Line(score.box.get_right(), keep.box.get_left(), color=GREEN, stroke_width=1.8)
    er = Line(score.box.get_right(), roll.box.get_left(), color=RED, stroke_width=1.8)
    scene.play(LaggedStart(Create(ek), Create(er), lag_ratio=0.2, run_time=0.7))
    scene.play(GrowFromCenter(keep), GrowFromCenter(roll), run_time=0.6)
    pulse_along(scene, [ek], GREEN, run_time=0.8)
    pulse_along(scene, [er], RED, run_time=0.8)
    scene.wait(hold)


# ── Beat: the fleet grid (local + remote sessions, a live sweep across them) ──
def grid_scene(scene: Scene, *, kicker="THE FLEET",
               caption="LOCAL AND REMOTE SESSIONS IN ONE GRID", folio="05 / 09", hold=1.6):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    labels = ["local · 01", "local · 02", "remote · ssh", "local · 03",
              "remote · ssh", "local · 04", "local · 05", "remote · ssh"]
    remote = {2, 4, 7}
    tiles = VGroup(*[dnode(t, w=2.7, h=1.0, accent=(ACCENT if i in remote else CYAN), fs=17)
                     for i, t in enumerate(labels)])
    tiles.arrange_in_grid(rows=2, cols=4, buff=0.55)
    _fit(tiles).move_to([0, -0.1, 0])
    scene.play(LaggedStart(*[GrowFromCenter(t) for t in tiles], lag_ratio=0.06, run_time=1.0))
    # a live sweep: each tile blooms briefly in reading order
    for t in tiles:
        g = _glow(t.box, t.accent, layers=5, spread=0.22, max_opacity=0.16)
        scene.play(FadeIn(g, run_time=0.08), FadeOut(g, run_time=0.12))
    scene.wait(hold)


# ── Beat: dispatch to a remote machine, watch + guide from one screen ──
def dispatch_scene(scene: Scene, *, kicker="DISPATCH",
                   caption="RUN ELSEWHERE, WATCH AND GUIDE FROM HERE", folio="05 / 09", hold=1.6):
    scene.camera.background_color = BG
    _frame(scene, kicker=kicker, caption=caption, folio=folio)
    here = dnode("your screen", w=3.2, h=1.25, accent=CYAN, sub="ONE PLACE", fs=23).move_to([-4.4, 0, 0])
    remote = dnode("remote machine", w=3.2, h=1.25, accent=ACCENT, sub="OVER SSH", fs=21).move_to([4.4, 0, 0])
    scene.play(FadeIn(_glow(here.box, CYAN)), GrowFromCenter(here),
               FadeIn(_glow(remote.box, ACCENT)), GrowFromCenter(remote), run_time=0.7)
    top = Line(here.box.get_right() + UP * 0.30, remote.box.get_left() + UP * 0.30, color=MUTE, stroke_width=1.8)
    bot = Line(remote.box.get_left() + DOWN * 0.30, here.box.get_right() + DOWN * 0.30, color=MUTE, stroke_width=1.8)
    tlab = label("TASK", 3, MUTE, 12).move_to([0, 0.62, 0])
    blab = label("STATUS · GUIDANCE", 2, MUTE, 12).move_to([0, -0.62, 0])
    scene.play(Create(top), Create(bot), FadeIn(tlab), FadeIn(blab), run_time=0.7)
    for _ in range(2):
        pulse_along(scene, [top], CYAN, run_time=0.8)
        pulse_along(scene, [bot], GREEN, run_time=0.8)
    scene.wait(hold)


# ── D2 (static, auto-laid-out architecture diagrams) ─────────────────
def render_d2(spec: str, out_png: Path, *, pad: int = 40, theme: int = 200) -> Path:
    if shutil.which('d2') is None:
        raise RuntimeError('d2 not installed. Run: brew install d2')
    d2f = out_png.with_suffix('.d2')
    d2f.write_text('vars: { d2-config: { theme-id: %d; pad: %d } }\n%s' % (theme, pad, spec), encoding='utf-8')
    subprocess.run(['d2', '--scale', '3', str(d2f), str(out_png)], check=True, capture_output=True)
    return out_png
