# Charon feature reel

A repeatable, committed pipeline that turns a manifest of Charon's features
into a cohesive set of short videos — rendered in parallel by Charon's own
shade swarm using the bundled `manim-video` skill.

The videos advertising Charon are made *by* Charon.

## Files

| File | Role |
|------|------|
| `features.yaml` | The manifest — one entry per video (slug, title, tagline, key points). Edit this to change the reel. |
| `brandkit.py` | Single source of visual truth — palette, fonts, depth (gradient + ambient glow), terminal **window chrome**, **glow/bloom**, animated **lower-thirds**, and the title card + outro. Imported by every clip; edit to restyle the whole reel at once. |
| `reel.py` | Driver — scaffolds per-video dirs, builds the `SpawnBatch` spec, stitches + **grades** clips. |

## The look (production)

- Clips render at **`-qh` (1080p60)**. Iterate at `-ql`, deliver `-qh`.
- `brandkit.py` v2 gives every clip depth and a real product feel: `gradient_bg`,
  `window_frame` (traffic-light chrome for framing UI), `glow`/`behind_glow`,
  `lower_third`, `transition_wipe`, and the glowing `brand_intro`/`brand_outro`.
- The stitch step applies a **color-safe cinematic grade** (`GRADE_VF`): gentle
  vignette + slight contrast/saturation. Deliberately no bloom screen-blend (shifts
  hue on dark frames) and no grain (casts color on near-black) — the in-scene glow
  already supplies the bloom. Grade a single clip with `python videos/reel.py grade <slug>`.

## Optimize a clip with the aesthetic judge

Charon's judge loop can hill-climb a clip's *look*. `/reel judge <slug>` spawns a
`quantitative` judge loop whose eval command is the **vision scorer**
(`aesthetic_judge.py`): it renders a contact sheet of the clip's key frames, a
vision model scores it 1–10 against `aesthetic_rubric.md`, and writes
`aesthetic_feedback.json` (score + concrete fixes). Each iteration, the implementer
shade reads that feedback, applies the single highest-impact fix to `script.py`,
re-renders, and the loop keeps improvements / rolls back regressions until it hits
the target score or runs out of iterations.

```
/reel judge video           # optimize the video clip (target 8.5)
/reel judge video 9.0 12    # target 9.0, up to 12 iterations
```

Vision backend (cloud opt-in): set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
(`CHARON_AESTHETIC_BACKEND` / `CHARON_AESTHETIC_MODEL` to override). Without a key
the scorer reports `no_backend` and the loop won't run — a local mlx-vlm backend
is the planned offline default. Score one clip ad-hoc:
`.venv/bin/python videos/aesthetic_judge.py <slug>`.

## Audio — narration + music

Per-scene **time-synced narration** via `manim-voiceover`: a content scene is a
`CharonVoiceScene` (`voice.py`) and each beat is wrapped so the animation length
follows the speech — `with self.voiceover(text=LINE) as tr: self.play(..., run_time=tr.duration)`.

```
bash videos/setup-audio.sh              # narration deps (+ --musicgen for local AI music)
/reel build                             # shades render narrated clips (narration: in manifest)
/reel stitch --music                    # generate a bed, duck it under the voice, mux
/reel stitch --music videos/audio/x.mp3 # use a specific track
```

Voice backend (`CHARON_VOICE_BACKEND`): `say` (offline macOS, default) · `mlx`
(your mlx-audio, set `CHARON_MLX_AUDIO_CMD`) · `elevenlabs`/`openai` (cloud opt-in).
Music backend (`videos/music.py`): a library track in `videos/audio/`, local
`musicgen`, or cloud `suno`. The bed is ducked under narration (sidechain) and
loudness-normalized at stitch.

## Roadmap (phased)

1. **Look** ✅ — brand kit v2 + 1080p60 + grade.
2. **Real UI capture** ✅ pipeline — VHS tapes + `capture`/`fit` framed inside
   `window_frame`. Needs `brew install vhs` (one-time).
3. **Audio** ✅ — time-synced narration (local `say`/`mlx` default, ElevenLabs/OpenAI
   opt-in) + AI/library music bed, ducked under the voice at stitch.
4. **Aesthetic judge** ✅ — `/reel judge <slug>` hill-climbs a clip's look via a
   vision scorer (needs a vision key).

Rendered output lands in `results/videos/<slug>/final.mp4` (gitignored), and the
stitched reel in `results/videos/charon-reel.mp4`.

## Use it (from the Charon TUI)

```
/reel list      # review the manifest
/reel build     # scaffold + spawn one shade per feature (parallel), using the brand kit
/batch <id>     # watch the swarm render (id is printed by /reel build)
/reel stitch    # concat finished clips into results/videos/charon-reel.mp4
```

Render a subset by slug: `/reel build video memory shades`.

## Reel ANY app — the director (`/reel plan`)

The Charon reel ships with a hand-curated `features.yaml`. For any *other* app, a
**director** writes the manifest for you: it studies the target, decides which
features are worth marketing, orders them into a narrative, and writes taglines +
beats + narration. You review, then render. (Propose-then-approve.)

```
/reel plan ~/Projects/some-app --reel some-app   # director writes videos/reels/some-app/features.yaml
# review/edit that manifest, then:
/reel build  --reel some-app                     # swarm renders its clips
/reel stitch --reel some-app --music             # -> results/reels/some-app/some-app-reel.mp4
/reel judge  --reel some-app dashboard           # optional aesthetic optimize
```

A **named reel** keeps its own manifest (`videos/reels/<name>/features.yaml`),
brand wordmark/tagline (auto-applied to its clips' intros), tapes, and output
(`results/reels/<name>/`). The built-in Charon reel needs no `--reel` and no `plan`.

**Where the intelligence is:** the director chooses the feature breakdown; each
shade designs its clip; the judge can polish the look. You stay in the loop at the
manifest (approve the story) and after build (review the clips).

## Use it (standalone, no daemon)

```
python videos/reel.py list
python videos/reel.py scaffold        # create dirs + copy brandkit
python videos/reel.py emit-batch      # print the SpawnBatch JSON
python videos/reel.py stitch          # stitch whatever clips exist
```

## How cohesion is guaranteed

Each shade renders independently and can't see the others, so without a shared
style you'd get nine unrelated clips. Two things keep the set coherent:

1. **`brandkit.py`** is copied next to every `script.py`; each clip does
   `from brandkit import *` and must use only its colors, font, `brand_intro`,
   and `brand_outro`.
2. The **batch constraints** (in `reel.py`) forbid ad-hoc palettes and enforce
   the intro/outro and target length on every task.

Change the look once in `brandkit.py`, re-run `/reel build`, and the entire reel
restyles consistently.

## Prerequisites

The `manim-video` skill must be set up once: `/skills setup manim-video`
(installs Manim + ffmpeg). LaTeX is not needed for these product clips.
