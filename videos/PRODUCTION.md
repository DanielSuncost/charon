# Charon video production system

A modular pipeline for high-end promotional videos, made by Charon itself.
Everything is local except the optional judge (your codex auth). Each stage is a
small module you can recombine.

## The stages (each is one module)

| Stage | Module | What it makes |
|-------|--------|---------------|
| **Script** | `scripts.md` + `script_judge.py` | Plain, earnest narration. The judge loop drives tone to a target score. |
| **Voice** | `voice.py` / `hero._mlx_say` | Local neural narration (Kokoro, `bm_lewis`). Robust to Kokoro's phoneme-length bug. |
| **Cards** | `editorial.py` | Editorial title card / feature card / outro (Didot + Helvetica, hairlines, folios). |
| **Output panels** | `render_panel.py` | A real artifact (manifest, judge JSON) as a large code window — `render_panel` (text) / `render_image_panel` (a captured pane). |
| **Real UI capture** | `termcap.py` | Drives the real Charon TUI in tmux, rasterizes panes at hi-res. |
| **3D motion** | `tilt3d.py` | Floating-screen tilt/pan with drop shadow; `crop_to_content` kills dead space. |
| **Transitions** | `transitions.py` | Named, composable transitions (fade, dissolve, wipe, slide, …) via xfade; returns segment timing. |
| **Music** | `hero.music_bed` | Synthesized ambient bed, ducked under narration. |
| **Grade** | `reel.GRADE_VF` | Color-safe cinematic finish (vignette + contrast). |
| **Judges** | `aesthetic_judge.py` (vision) · `script_judge.py` (tone) | Score frames/scripts via codex; loop until they pass. |

## Standard structure (every video)

Every feature video opens the same way so viewers always know what they're about
to watch:

1. **Topic intro** (`editorial.video_intro`) — brand masthead + the video's title
   (Didot) + a one-line thesis of what it covers, narrated with an opening hook.
   This is required on every video.
2. **Content beats** — one per script line, each showing a real output, tilted.
3. **Outro** — the sign-off.

`video_intro(title=..., thesis=..., number=...)` auto-fits the title and thesis to
the margins, so any feature's text works without overflow.

## Composing a video

`feature_beats.py` shows the pattern end-to-end:

1. Build per-beat clips — each is a **real output** (panel or captured pane), made
   into a still, then `tilt3d.render`'d with its own pan.
2. Narrate each script line (`_mlx_say`), timed to its beat.
3. `transitions.chain(clips, out, specs=[...])` → composed video + segment start times.
4. Align each narration line to its segment start; duck a music bed under it.
5. Grade, mux, faststart.

### Transitions

```python
from transitions import chain, TRANSITIONS   # cut, fade, dissolve, fadeblack,
                                              # wipe{left,right,up,down}, slide*,
                                              # smooth*, circleopen/close, radial, pixelize
final, starts = chain(clips, out, specs=[('fadeblack', 0.5), ('dissolve', 0.6)])
```

`specs[i]` is the transition between clip `i` and `i+1` (name, seconds). `starts[i]`
is when clip `i` begins on the composed timeline — used to place narration.

## Quality gate (the loops)

- **Tone:** `python videos/script_judge.py loop` — judge → rewrite → re-judge to a target.
- **Look:** `aesthetic_judge.score_image(frame)` per beat; apply the returned fixes, re-render, re-score.

Both run through your codex auth (no API key). The lesson the visual loop taught:
**show the feature's output, not the command** — beats are real artifacts.

## To make a new feature video

1. Add/confirm the script in `scripts.md` (run the tone loop).
2. Define its beats (which real output each shows) and transitions.
3. Build; score the frames; iterate on the flagged beats.

The four other scripts (Memory, Shades, Judge Loops, The Fleet) are written and
ready to template the same way.
