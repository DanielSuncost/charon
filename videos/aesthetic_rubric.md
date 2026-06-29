# Charon reel — aesthetic rubric

Score a rendered frame (or contact sheet) of a feature video on a **1–10** scale.
This rubric is the judge's standard and the implementer's target. Be a harsh,
specific critic: 7 is "good", 8.5 is "ship it", 9.5+ is rare.

## Criteria (weight)

1. **Brand cohesion (0.25)** — Uses only the kit palette (cyan `#22D3EE`,
   amber `#FFB020`, green, near-black bg, panel greys) and the Menlo mono font.
   No ad-hoc colors. Wordmark/intro/outro consistent with the rest of the reel.
2. **Composition & hierarchy (0.20)** — Clear focal point, deliberate negative
   space, aligned to a grid, balanced. Nothing crammed in a corner or floating.
3. **Depth & finish (0.15)** — Gradient/ambient depth (not flat black), tasteful
   glow on accents, window chrome where UI is shown, subtle vignette. Looks
   designed, not like default slides.
4. **Legibility (0.15)** — Text is large enough, high-contrast, not overlapping
   shapes; edges have ≥0.5 buffer; nothing clipped at frame edges.
5. **Motion quality (0.15)** — (video) eased/staggered reveals, breathing room
   after key beats, no abrupt pops. (still) implied by clean spacing/timing.
6. **Message clarity (0.10)** — A viewer grasps the feature's point from the
   visuals, not just captions. The "aha" is shown, not told.

## Output

Return ONLY a JSON object:

```json
{
  "score": 7.5,
  "criteria": {"brand": 8, "composition": 7, "depth": 8, "legibility": 7, "motion": 7, "clarity": 8},
  "feedback": "One short paragraph: the single highest-impact fix to raise the score.",
  "fixes": ["concrete, code-level change 1", "change 2", "change 3"]
}
```

`fixes` must be specific and actionable for someone editing the Manim `script.py`
(e.g. "increase title font_size to 52 and move up 0.4", "add glow() behind the
db node", "the arrow overlaps the panel edge — inset the row by 0.6").
