"""Charon feature-reel brand kit (v2) — the single source of visual truth.

Every generated video imports this module so the whole reel looks like one
campaign: same palette, typography, depth, motion, title card and outro.
A copy of this file sits next to each video's script.py, so
`from brandkit import *` works under Manim's default (script-dir) import path.

v2 adds production-grade primitives: gradient depth, an ambient spotlight,
terminal window chrome (for framing real UI capture), glow/bloom, animated
lower-thirds, and eased/staggered intro+outro. Edit here to restyle the
*entire* reel at once. Render production clips at -qh (1080p60).
"""
from manim import *

# ── Palette ──────────────────────────────────────────────────────────
BG = "#070708"          # near-black base
BG_TOP = "#0E1116"      # gradient top (cool, slightly lifted)
BG_BOTTOM = "#050506"    # gradient bottom (deep)
PANEL = "#12161C"        # window-chrome fill
PANEL_EDGE = "#222A33"   # window-chrome border
PRIMARY = "#22D3EE"     # cyan — primary structure / titles
ACCENT = "#FFB020"      # amber — emphasis / the "aha"
SECONDARY = "#7CE38B"   # green — secondary / success
MUTED = "#5A6472"       # grey-blue — axes, de-emphasized, body text
TEXT = "#E8ECF1"        # near-white body text

# Traffic-light dots for window chrome.
DOT_RED = "#FF5F57"
DOT_YEL = "#FEBC2E"
DOT_GRN = "#28C840"

# ── Typography ───────────────────────────────────────────────────────
MONO = "Menlo"          # monospace everywhere (clean Pango kerning)
TITLE = 52
HEADING = 36
BODY = 28
LABEL = 24
CAPTION = 20

# ── Timing (seconds) ─────────────────────────────────────────────────
T_TITLE = 1.4
T_REVEAL = 1.6
T_TRANSFORM = 1.3
W_KEY = 1.2             # wait after a key reveal

WORDMARK = "CHARON"


# ── Depth & background ───────────────────────────────────────────────
def gradient_bg(scene: Scene) -> VGroup:
    """Full-frame vertical gradient + soft ambient spotlight. Call FIRST.

    Returns the background group (already added) in case you want to keep it
    on screen across a FadeOut(*self.mobjects) by excluding it.
    """
    scene.camera.background_color = BG
    rect = Rectangle(
        width=config.frame_width + 0.2, height=config.frame_height + 0.2,
        stroke_width=0,
    ).set_fill(opacity=1).set_color_by_gradient(BG_TOP, BG_BOTTOM)
    halo = ambient_glow(PRIMARY, radius=4.2, layers=22, max_opacity=0.05).shift(UP * 0.6)
    bg = VGroup(rect, halo)
    scene.add(bg)
    return bg


def ambient_glow(color: str = PRIMARY, radius: float = 3.5, layers: int = 30,
                 max_opacity: float = 0.05) -> VGroup:
    """Soft radial spotlight built from many stacked translucent circles.

    Many thin layers + a quadratic falloff keep it band-free.
    """
    g = VGroup()
    for i in range(layers, 0, -1):
        r = radius * (i / layers)
        op = max_opacity * (1 - (i / layers)) ** 2.2
        g.add(Circle(radius=r, stroke_width=0).set_fill(color, opacity=op))
    return g


def behind_glow(mob: Mobject, color: str = PRIMARY, pad: float = 1.7,
                layers: int = 26, max_opacity: float = 0.10) -> VGroup:
    """Soft radial halo sized to a mobject — safe for TEXT (no glyph artifacts).

    Unlike glow(), this never copies the mobject's shape, so it won't produce
    boxy ghosts around letters. Use for wordmarks/titles; use glow() for solids.
    """
    g = ambient_glow(color, radius=max(mob.width, mob.height) * 0.5 * pad,
                     layers=layers, max_opacity=max_opacity)
    g.stretch_to_fit_width(mob.width * pad)
    g.stretch_to_fit_height(mob.height * pad * 1.6)
    g.move_to(mob)
    return g


def glow(mob: Mobject, color: str | None = None, layers: int = 8,
         spread: float = 0.18, max_opacity: float = 0.30) -> VGroup:
    """Bloom halo around a mobject — stack scaled, translucent copies behind it."""
    color = color or PRIMARY
    halo = VGroup()
    for i in range(layers, 0, -1):
        c = mob.copy().set_stroke(width=0).set_fill(color, opacity=max_opacity * (i / layers) / layers * 2)
        c.scale(1 + spread * (i / layers))
        c.set_color(color)
        c.set_fill(color, opacity=max_opacity * (1 - i / layers))
        halo.add(c)
    halo.move_to(mob)
    return halo


# ── Chrome ───────────────────────────────────────────────────────────
def window_frame(width: float = 9.0, height: float = 5.0, title: str = "charon") -> VGroup:
    """A terminal window with traffic-light dots and a title bar.

    Use to frame real UI capture or content. The inner content area is the
    rectangle returned as group[-1]; place footage/mobjects over it.
    """
    body = RoundedRectangle(corner_radius=0.16, width=width, height=height,
                            stroke_color=PANEL_EDGE, stroke_width=2).set_fill(PANEL, opacity=1)
    bar_h = 0.5
    bar = RoundedRectangle(corner_radius=0.16, width=width, height=bar_h,
                           stroke_width=0).set_fill(PANEL_EDGE, opacity=0.45)
    bar.align_to(body, UP)
    dots = VGroup(*[Dot(radius=0.07, color=c) for c in (DOT_RED, DOT_YEL, DOT_GRN)])
    dots.arrange(RIGHT, buff=0.16).next_to(bar.get_left(), RIGHT, buff=0.3)
    label = Text(title, font=MONO, color=MUTED, font_size=CAPTION).move_to(bar)
    inner = Rectangle(width=width - 0.3, height=height - bar_h - 0.2, stroke_width=0,
                      fill_opacity=0).next_to(bar, DOWN, buff=0.1)
    return VGroup(body, bar, dots, label, inner)


def lower_third(scene: Scene, title: str, subtitle: str = "", play: bool = True) -> VGroup:
    """Animated bottom-left caption bar with an accent tick."""
    tick = Rectangle(width=0.08, height=0.7, stroke_width=0).set_fill(ACCENT, opacity=1)
    t = Text(title, font=MONO, weight=BOLD, color=TEXT, font_size=HEADING)
    grp_top = VGroup(tick, t).arrange(RIGHT, buff=0.3, aligned_edge=DOWN)
    parts = [grp_top]
    if subtitle:
        s = Text(subtitle, font=MONO, color=MUTED, font_size=LABEL)
        parts.append(s)
    block = VGroup(*parts).arrange(DOWN, buff=0.18, aligned_edge=LEFT)
    block.to_corner(DL, buff=0.8)
    if play:
        scene.play(
            LaggedStart(
                GrowFromEdge(tick, DOWN),
                FadeIn(t, shift=RIGHT * 0.25),
                *( [FadeIn(parts[1], shift=UP * 0.15)] if subtitle else [] ),
                lag_ratio=0.3, run_time=1.0,
            )
        )
    return block


# ── Title card + outro (identical across the reel) ───────────────────
def wordmark(scale: float = 1.0, color: str = PRIMARY) -> MarkupText:
    return MarkupText(
        f'<span letter_spacing="5000">{WORDMARK}</span>',
        font=MONO, weight=BOLD, color=color, font_size=int(TITLE * scale),
    )


def brand_intro(scene: Scene, title: str, subtitle: str = "") -> None:
    """Standard animated title card. Identical across the whole reel.

    First scene of every feature video. Gradient depth, glowing wordmark,
    staggered eased reveal.
    """
    gradient_bg(scene)
    mark = wordmark(0.62).to_edge(UP, buff=0.85)
    mark_glow = behind_glow(mark, PRIMARY, pad=2.0, max_opacity=0.12)

    name = Text(title, font=MONO, weight=BOLD, color=TEXT, font_size=TITLE)
    rule = Line(LEFT, RIGHT, color=ACCENT).set_width(0.1).next_to(name, DOWN, buff=0.32)
    group = VGroup(name, rule).move_to(ORIGIN)

    scene.play(FadeIn(mark_glow), FadeIn(mark, shift=DOWN * 0.2), run_time=0.9)
    scene.play(
        Write(name, run_time=T_TITLE),
        rule.animate(run_time=T_TITLE, rate_func=smooth).set_width(name.width + 0.8),
    )
    if subtitle:
        sub = Text(subtitle, font=MONO, color=ACCENT, font_size=LABEL).next_to(rule, DOWN, buff=0.42)
        scene.play(FadeIn(sub, shift=UP * 0.2), run_time=0.8)
    scene.wait(W_KEY)
    scene.play(FadeOut(Group(*scene.mobjects)), run_time=0.55)


def brand_outro(scene: Scene, tagline: str = "An agent OS for your local machine.") -> None:
    """Standard sign-off. Identical across the whole reel. Last scene."""
    gradient_bg(scene)
    mark = wordmark(1.05)
    mark_glow = behind_glow(mark, PRIMARY, pad=2.1, max_opacity=0.14)
    line = Text(tagline, font=MONO, color=ACCENT, font_size=LABEL).next_to(mark, DOWN, buff=0.45)
    dot = Dot(color=ACCENT, radius=0.05).next_to(line, RIGHT, buff=0.15)
    scene.play(FadeIn(mark_glow), FadeIn(mark, scale=1.06), run_time=T_TITLE)
    scene.play(FadeIn(line, shift=UP * 0.2), FadeIn(dot), run_time=0.8)
    scene.wait(W_KEY + 0.6)
    scene.play(FadeOut(Group(*scene.mobjects)), run_time=0.65)


def transition_wipe(scene: Scene, color: str = ACCENT) -> None:
    """Quick accent wipe — use between major beats for a kinetic cut."""
    bar = Rectangle(width=config.frame_width + 1, height=config.frame_height + 1,
                    stroke_width=0).set_fill(color, opacity=1)
    bar.next_to(LEFT * config.frame_width, LEFT, buff=0)
    scene.play(bar.animate.move_to(ORIGIN), run_time=0.28, rate_func=rush_into)
    scene.play(bar.animate.next_to(RIGHT * config.frame_width, RIGHT, buff=0),
               run_time=0.28, rate_func=rush_from)
    scene.remove(bar)


def caption(scene: Scene, text: str, duration: float = 2.0) -> None:
    """Add a subtitle for the current beat (also feeds .srt if enabled)."""
    scene.add_subcaption(text, duration=duration)
