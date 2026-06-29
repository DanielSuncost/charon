# Manim Video Skill

A bundled Charon skill: a production pipeline for mathematical and technical
animations using [Manim Community Edition](https://www.manim.community/).

## What it does

Creates 3Blue1Brown-style animated videos from a text prompt. A Charon agent
runs the full pipeline — creative planning, Python code generation, rendering,
scene stitching, and iterative refinement — and delivers an MP4. No API call:
the agent writes the code, renders the scenes, and stitches them itself.

## Use cases

- **Product / launch videos** — "Make a 60s launch video for our new feature"
- **Concept explainers** — "Explain how neural networks learn"
- **Equation derivations** — "Animate the proof of the Pythagorean theorem"
- **Algorithm visualizations** — "Show how quicksort works step by step"
- **Data stories** — "Animate our before/after performance metrics"
- **Architecture diagrams** — "Show our microservice architecture building up"

## Onboarding

One command installs everything into Charon's environment (macOS + Ubuntu):

```bash
bash skills/manim-video/scripts/setup.sh               # install deps
bash skills/manim-video/scripts/setup.sh --check       # verify only
bash skills/manim-video/scripts/setup.sh --with-latex  # also install LaTeX (for MathTex)
```

Or from the Charon TUI: `/skills setup manim-video`.

## Prerequisites

Python 3.10+, Manim CE v0.20+, ffmpeg, and the cairo/pango system libraries.
LaTeX is optional and only needed for animated math equations (`MathTex`).

## How an agent uses it

The agent discovers this skill via the `Skills` tool (`Skills(list)` →
`Skills(view manim-video)`), reads `SKILL.md`, pulls in the relevant
`references/*.md` as needed, then plans → codes → renders → stitches.
