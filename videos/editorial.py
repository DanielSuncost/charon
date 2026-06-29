"""Charon reel — editorial brand system (v3).

Design language: IDEA magazine (Swiss/Japanese typographic precision — grids,
hairlines, section numerals, tracked caps) × Hermès (warm restraint, luxe
negative space). Flat and typographic — no glow. Display serif is Didot;
labels are tracked Helvetica Neue; technical captions stay Menlo mono.

Import in a scene file: `from editorial import *`
"""
from manim import *

# ── Palette ──────────────────────────────────────────────────────────
BG = "#0B0A09"        # warm near-black
PAPER = "#ECE7DE"     # warm paper white (display + body)
MUTE = "#857C70"      # warm grey (secondary)
FAINT = "#3A352E"     # hairline grey
ACCENT = "#E0852A"    # single warm accent (Hermès), used sparingly

# ── Type ─────────────────────────────────────────────────────────────
SERIF = "Didot"            # luxe magazine display
SANS = "Helvetica Neue"    # Swiss tracked labels / numerals
MONO = "Menlo"             # technical / terminal captions only

# ── Layout (16:9 editorial margins) ──────────────────────────────────
def margins():
    fw, fh = config.frame_width, config.frame_height
    return (-fw / 2 + 1.1, fw / 2 - 1.1, fh / 2 - 0.7, -fh / 2 + 0.7)  # L,R,T,B


def trk(s: str, sp: int = 6) -> str:
    return f'<span letter_spacing="{sp * 1000}">{s}</span>'


def label(s: str, sp: int = 6, color=PAPER, size=18, weight=NORMAL):
    return MarkupText(trk(s, sp), font=SANS, color=color, font_size=size, weight=weight)


def hairline(x0, x1, y, color=FAINT, w=1.0):
    return Line([x0, y, 0], [x1, y, 0], color=color, stroke_width=w)


def bg(scene: Scene):
    scene.camera.background_color = BG


# ── Feature title card ───────────────────────────────────────────────
def feature_card(scene: Scene, *, number: str, title: str, kicker: str = "FEATURE",
                 subtitle: str = "", index: str = "", folio: str = "01 / 09", hold: float = 1.0,
                 **_legacy):
    """Animated editorial title card. Fast, precise, elegant.

    `index` is an optional vertical tracked label set against a hairline on the
    right margin (a Swiss/IDEA structural device — replaces the old JP accent).
    """
    bg(scene)
    L, R, T, B = margins()

    top = hairline(L, R, T)
    bot = hairline(L, R, B)
    mast = label("CHARON", 6, PAPER, 18, BOLD).next_to(top, UP, buff=0.14).align_to(top, LEFT)
    mast_r = label("AGENT OPERATING SYSTEM", 5, MUTE, 13).next_to(top, UP, buff=0.16).align_to(top, RIGHT)

    num = Text(number, font=SANS, color=PAPER, font_size=46, weight=THIN)
    num.next_to(top, DOWN, buff=0.55).align_to(top, LEFT)
    kick = label(kicker, 5, ACCENT, 13).next_to(num, RIGHT, buff=0.25, aligned_edge=DOWN)

    disp = Text(title, font=SERIF, color=PAPER, font_size=108)
    disp.move_to([L + disp.width / 2, -0.35, 0])
    rule = hairline(L, L + 3.2, -1.55, ACCENT, 2)
    sub = label(subtitle, 4, MUTE, 16).next_to(rule, DOWN, buff=0.22).align_to(rule, LEFT) if subtitle else None

    # Right-margin device: a thin vertical hairline + small rotated tracked label.
    vrule = vlab = None
    if index:
        vrule = Line([R, T - 0.35, 0], [R, 0.2, 0], color=FAINT, stroke_width=1)
        vlab = label(index, 4, MUTE, 13).rotate(PI / 2).next_to(vrule, LEFT, buff=0.18)

    fol_l = label("CHARON — MMXXVI", 4, MUTE, 13).next_to(bot, DOWN, buff=0.16).align_to(bot, LEFT)
    fol_r = label(folio, 4, MUTE, 13).next_to(bot, DOWN, buff=0.16).align_to(bot, RIGHT)

    # Motion: rules draw, labels settle, title masks up, accent draws — quick & eased.
    scene.play(Create(top), Create(bot), run_time=0.55, rate_func=smooth)
    scene.play(LaggedStart(FadeIn(mast, shift=RIGHT * 0.15), FadeIn(mast_r, shift=LEFT * 0.15),
                           lag_ratio=0.2, run_time=0.45))
    scene.play(LaggedStart(FadeIn(num, shift=UP * 0.15), FadeIn(kick, shift=RIGHT * 0.15),
                           lag_ratio=0.3, run_time=0.45))
    scene.play(FadeIn(disp, shift=UP * 0.22), run_time=0.7, rate_func=smooth)
    extras = [Create(rule)]
    if sub:
        extras.append(FadeIn(sub, shift=UP * 0.1))
    if vrule:
        extras.extend([Create(vrule), FadeIn(vlab, shift=LEFT * 0.1)])
    scene.play(LaggedStart(*extras, lag_ratio=0.22, run_time=0.6))
    scene.play(FadeIn(fol_l), FadeIn(fol_r), run_time=0.35)
    scene.wait(hold)


def card_out(scene: Scene, run_time: float = 0.5):
    scene.play(FadeOut(Group(*scene.mobjects)), run_time=run_time)


# ── Intro / Outro ────────────────────────────────────────────────────
def video_intro(scene: Scene, *, title: str, thesis: str, kicker: str = "CHARON — FEATURE FILM",
                number: str = "", hold: float = 2.0):
    """Standard opening that tells the viewer what the video is about.

    Small brand masthead, the video's topic as a Didot title, and a one-line
    thesis. Used at the head of every feature video.
    """
    bg(scene)
    L, R, T, B = margins()
    top = hairline(L, R, T)
    bot = hairline(L, R, B)
    mast = label(kicker, 6, MUTE, 14).next_to(top, DOWN, buff=0.34).align_to(top, LEFT)
    num = Text(number, font=SANS, color=MUTE, font_size=18, weight=THIN).next_to(
        top, DOWN, buff=0.3).align_to(top, RIGHT) if number else None

    max_w = (R - L)
    disp = Text(title, font=SERIF, color=PAPER, font_size=100).move_to([0, 0.35, 0])
    if disp.width > max_w:
        disp.scale(max_w / disp.width)
    rule = hairline(-disp.width / 2, disp.width / 2, -0.85, ACCENT, 2)
    th = label(thesis, 3, MUTE, 18).next_to(rule, DOWN, buff=0.32)
    if th.width > max_w:
        th.scale(max_w / th.width).next_to(rule, DOWN, buff=0.32)
    fol = label("PRESENTED BY CHARON", 4, MUTE, 12).next_to(bot, UP, buff=0.22)

    scene.play(Create(top), Create(bot), run_time=0.55, rate_func=smooth)
    scene.play(LaggedStart(FadeIn(mast, shift=DOWN * 0.1),
                           *([FadeIn(num, shift=DOWN * 0.1)] if num else []),
                           lag_ratio=0.2, run_time=0.45))
    scene.play(Write(disp), run_time=1.1)
    scene.play(Create(rule), FadeIn(th, shift=UP * 0.1), run_time=0.6)
    scene.play(FadeIn(fol), run_time=0.3)
    scene.wait(hold)
    card_out(scene, 0.55)


def intro(scene: Scene, tagline: str = "An agent operating system for your local machine."):
    bg(scene)
    L, R, T, B = margins()
    top = hairline(L, R, T)
    bot = hairline(L, R, B)
    word = Text("CHARON", font=SERIF, color=PAPER, font_size=120)
    word.move_to([0, 0.15, 0])  # Didot wordmark reads best solid and centered
    rule = hairline(-2.4, 2.4, -1.05, ACCENT, 2)
    tag = label(tagline, 3, MUTE, 18).next_to(rule, DOWN, buff=0.3)
    kicker = label("AN OPERATING SYSTEM FOR AGENTS", 5, MUTE, 13).next_to(top, DOWN, buff=0.32)
    scene.play(Create(top), Create(bot), run_time=0.55, rate_func=smooth)
    scene.play(FadeIn(kicker, shift=DOWN * 0.1), run_time=0.4)
    scene.play(FadeIn(word, shift=UP * 0.2), run_time=0.9, rate_func=smooth)
    scene.play(Create(rule), FadeIn(tag, shift=UP * 0.1), run_time=0.6)
    scene.wait(1.0)
    card_out(scene, 0.55)


def outro(scene: Scene, line: str = "Everything runs locally. You own the data."):
    bg(scene)
    word = Text("CHARON", font=SERIF, color=PAPER, font_size=104).move_to([0, 0.25, 0])
    rule = hairline(-2.1, 2.1, -0.85, ACCENT, 2)
    sub = label(line, 3, ACCENT, 16).next_to(rule, DOWN, buff=0.3)
    scene.play(FadeIn(word, shift=UP * 0.15), run_time=0.8, rate_func=smooth)
    scene.play(Create(rule), FadeIn(sub, shift=UP * 0.1), run_time=0.6)
    scene.wait(1.4)
    card_out(scene, 0.6)


# ── Editorial frame for real TUI footage ─────────────────────────────
def frame_caption(scene: Scene, *, number: str, title: str, detail: str = ""):
    """Lay an editorial masthead/caption over (composited) TUI footage.

    Returns the mobjects so the caller can time them; nothing is removed.
    """
    bg_keep = scene.camera.background_color
    L, R, T, B = margins()
    top = hairline(L, R, T)
    mast = label("CHARON", 6, PAPER, 16, BOLD).next_to(top, UP, buff=0.12).align_to(top, LEFT)
    num = Text(number, font=SANS, color=MUTE, font_size=20, weight=THIN).next_to(top, UP, buff=0.1).align_to(top, RIGHT)
    cap_num = label(title.upper(), 5, ACCENT, 13)
    cap = label(detail, 3, MUTE, 13)
    block = VGroup(cap_num, cap).arrange(DOWN, buff=0.12, aligned_edge=LEFT)
    block.next_to(hairline(L, R, B), UP, buff=0.18).align_to(L, LEFT)
    grp = VGroup(top, mast, num, block)
    scene.play(Create(top), FadeIn(mast), FadeIn(num), FadeIn(block, shift=UP * 0.1), run_time=0.6)
    return grp
