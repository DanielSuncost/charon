# Design Library Spec v0.1

## Goal
Build a structured corpus of high-quality UI/design references that can later support:
- inspiration
- pattern discovery
- internal design system thinking
- retrieval
- clustering by design language
- future model training / fine-tuning for a design AI agent

This library should optimize for:
- high signal
- consistent annotation
- easy browsing
- future machine-readability
- promotion from raw inspiration → curated pattern asset

---

## 1. Collection model

Think in 4 layers:

### Layer 1 — Raw Sources
Where the design came from.
Examples:
- X posts
- Dribbble shots
- Behance projects
- product websites
- app screenshots
- design galleries
- videos / motion captures

### Layer 2 — Examples
A single concrete UI artifact or post.
Examples:
- one landing page screenshot
- one dashboard screen
- one onboarding flow clip
- one interaction demo post

### Layer 3 — Patterns
Abstracted reusable ideas derived from examples.
Examples:
- sticky left rail + dense content grid
- progressive disclosure filter panel
- layered hero with ambient motion
- compact KPI strip above analytical table

### Layer 4 — Design Languages
Higher-level stylistic families.
Examples:
- restrained productivity
- playful consumer
- cinematic marketing
- AI-native workspace
- modern fintech
- editorial minimalism

---

## 2. Core entities

### A. Source
Represents the origin.

Fields:
- `source_id`
- `source_type`
  - x
  - website
  - dribbble
  - behance
  - mobbin
  - app
  - repo
  - video
  - other
- `name`
- `url`
- `author_or_brand`
- `platform_handle`
- `credibility`
- `notes`

Example:

```json
{
  "source_id": "src_x_damngoodui",
  "source_type": "x",
  "name": "Damn Good UI",
  "url": "https://x.com/DamnGoodUI",
  "author_or_brand": "Damn Good UI",
  "platform_handle": "@DamnGoodUI",
  "credibility": "high",
  "notes": "Curated account posting polished UI interaction examples."
}
```

### B. Example
Represents one specific design artifact.

Fields:
- `example_id`
- `source_id`
- `title`
- `source_url`
- `canonical_url`
- `creator`
- `product_or_brand`
- `captured_at`
- `surface_type`
- `platform`
- `image_paths`
- `video_paths`
- `thumbnail_path`
- `raw_text`
- `summary`
- `why_notable`
- `quality_score`
- `training_candidate`
- `status`

Enums:

#### `surface_type`
- landing_page
- dashboard
- settings
- onboarding
- auth
- profile
- search
- feed
- editor
- e_commerce
- mobile_screen
- marketing_site
- data_viz
- form
- nav_system
- modal_flow
- unknown

#### `platform`
- web
- ios
- android
- desktop
- responsive
- unknown

#### `status`
- inbox
- annotated
- pattern_extracted
- shortlisted
- approved
- rejected
- duplicate

Example:

```json
{
  "example_id": "ex_damngoodui_2026_03_29_001",
  "source_id": "src_x_damngoodui",
  "title": "Clean set of interactions by kail_designs",
  "source_url": "https://x.com/DamnGoodUI/status/2038384868724187434",
  "canonical_url": "https://x.com/DamnGoodUI/status/2038384868724187434",
  "creator": "@kail_designs",
  "product_or_brand": null,
  "captured_at": "2026-04-10T00:00:00Z",
  "surface_type": "unknown",
  "platform": "unknown",
  "image_paths": [],
  "video_paths": [],
  "thumbnail_path": null,
  "raw_text": "Clean set of interactions by @kail_designs",
  "summary": "Short interaction showcase featuring polished UI transitions and refined motion.",
  "why_notable": "High interaction polish and concise visual clarity.",
  "quality_score": 8.4,
  "training_candidate": true,
  "status": "inbox"
}
```

### C. Annotation
Structured tags and observations attached to an example.

Fields:
- `annotation_id`
- `example_id`
- `visual_style_tags`
- `interaction_tags`
- `component_tags`
- `layout_tags`
- `ux_tags`
- `motion_tags`
- `design_language_tags`
- `strengths`
- `weaknesses`
- `novelty`
- `production_likelihood`
- `annotation_confidence`
- `annotator`
- `annotated_at`

Suggested tag families:

#### Visual style tags
- minimal
- premium
- soft
- high_contrast
- monochrome
- glassmorphism
- neumorphic
- brutalist
- editorial
- futuristic
- enterprise
- playful
- luxury
- developer_tooling
- fintech
- ai_native
- tactile

#### Interaction tags
- hover_reveal
- drag_and_drop
- progressive_disclosure
- animated_transition
- snap_scroll
- parallax
- inline_edit
- command_palette
- expandable_panel
- multi_step_flow
- gesture_navigation
- swipe_action

#### Component tags
- sidebar
- top_nav
- command_bar
- table
- kanban
- card_grid
- chart
- modal
- drawer
- tabs
- segmented_control
- timeline
- chat_panel
- activity_feed
- filter_bar
- search_input
- empty_state
- toast
- tooltip

#### Layout tags
- bento
- split_pane
- centered_single_column
- asymmetrical_hero
- dense_dashboard
- card_masonry
- multi_panel_workspace
- floating_overlay
- full_bleed
- editorial_grid

#### UX tags
- high_scanability
- low_cognitive_load
- expert_focused
- beginner_friendly
- strong_hierarchy
- weak_affordance
- high_feedback
- compact_density
- spacious_density
- strong_empty_state
- discoverable
- low_discoverability

#### Motion tags
- subtle_motion
- springy
- cinematic
- utilitarian
- microinteraction_heavy
- choreographed_scroll
- stateful_transition

### D. Pattern
A reusable design/interaction pattern distilled from examples.

Fields:
- `pattern_id`
- `name`
- `description`
- `pattern_type`
- `applicability`
- `strengths`
- `failure_modes`
- `example_ids`
- `design_language_tags`
- `implementation_notes`
- `training_value`
- `status`

Pattern types:
- layout
- navigation
- component
- interaction
- motion
- onboarding
- content_presentation
- data_density
- conversion
- workflow

Example:

```json
{
  "pattern_id": "pat_progressive_context_panel",
  "name": "Progressive Context Panel",
  "description": "A secondary panel that appears only when deeper detail is needed, preserving default simplicity while enabling expert workflows.",
  "pattern_type": "interaction",
  "applicability": ["dashboard", "editor", "workspace"],
  "strengths": [
    "Balances simplicity and power",
    "Reduces default clutter",
    "Supports expert exploration"
  ],
  "failure_modes": [
    "Can hide important controls",
    "May reduce discoverability"
  ],
  "example_ids": [],
  "design_language_tags": ["ai_native", "productivity", "enterprise_modern"],
  "implementation_notes": "Works best with strong row/card selection states and smooth panel transitions.",
  "training_value": 9,
  "status": "active"
}
```

### E. Design Language
A cluster/family of related stylistic traits.

Fields:
- `design_language_id`
- `name`
- `description`
- `traits`
- `anti_traits`
- `representative_example_ids`
- `related_pattern_ids`

Example:

```json
{
  "design_language_id": "dl_restrained_productivity",
  "name": "Restrained Productivity",
  "description": "Low-noise interfaces with tight hierarchy, muted palette, precise spacing, and high utility density.",
  "traits": [
    "Muted color system",
    "Strong spacing rhythm",
    "Sparse but clear iconography",
    "High information efficiency"
  ],
  "anti_traits": [
    "Playful illustration-first layouts",
    "Overt decorative motion"
  ],
  "representative_example_ids": [],
  "related_pattern_ids": []
}
```

---

## 3. Curation stages

Each example should move through a pipeline:

### Stage 0 — Inbox
Unprocessed capture.
- link saved
- screenshot/video saved if possible
- no deep annotation yet

### Stage 1 — Basic annotation
Add:
- surface type
- platform
- major tags
- short summary
- quality score

### Stage 2 — Deep annotation
Add:
- strengths / weaknesses
- design language guess
- interaction notes
- components present
- novelty assessment

### Stage 3 — Pattern extraction
Ask:
- what reusable pattern exists here?
- what makes this distinct?
- should it form or reinforce a pattern entry?

### Stage 4 — Promotion
Mark example as:
- approved for curated library
- rejected
- duplicate
- reference-only

### Stage 5 — Training-set candidate
Only promote if:
- visuals are clear
- pattern signal is strong
- duplication is low
- quality is high
- labeling is good enough

---

## 4. Scoring system

Use a 1–10 or 0–100 score. Simpler is better at first.

Recommended subscores:
- `visual_quality`
- `interaction_quality`
- `pattern_usefulness`
- `originality`
- `clarity`
- `completeness`
- `annotation_quality`
- `training_suitability`

And one aggregate:
- `quality_score`

Suggested promotion thresholds:
- `>= 8.5` → shortlist
- `>= 9.0` → approved / training candidate
- `< 7.0` → probably reference-only unless uniquely novel

---

## 5. File/folder structure

Suggested repo structure:

```text
design-library/
  README.md
  schema/
    source.schema.json
    example.schema.json
    annotation.schema.json
    pattern.schema.json
    design-language.schema.json
  sources/
    sources.json
  examples/
    inbox/
    annotated/
    approved/
    rejected/
  assets/
    images/
    videos/
    thumbnails/
  patterns/
    patterns.json
  design-languages/
    design-languages.json
  indexes/
    tags.json
    examples-by-source.json
    examples-by-pattern.json
```

If you want a Markdown-friendly system instead of pure JSON:

```text
design-library/
  sources/
    damn-good-ui.md
  examples/
    inbox/
      ex_damngoodui_2026_03_29_001.md
  patterns/
    progressive-context-panel.md
  design-languages/
    restrained-productivity.md
  assets/
    ...
```

Recommendation:
- JSON for machine-readable records
- Markdown for human-readable notes
- assets stored locally
- stable IDs everywhere

---

## 6. Minimal metadata standard

Every example must have at least:
- `example_id`
- `source_url`
- `source_id`
- `title`
- `captured_at`
- `surface_type`
- `platform`
- `summary`
- `visual_style_tags`
- `component_tags`
- `interaction_tags`
- `quality_score`
- `status`

This is the minimum viable corpus.

---

## 7. Annotation rules

Rules:
- Prefer specific tags over vague praise
- Do not tag “beautiful” or “cool”; tag actual properties
- Separate style from interaction
- Separate product category from design language
- Note uncertainty explicitly
- Mark duplicates aggressively
- Avoid overfitting to trend aesthetics
- Capture why something works, not just what it looks like

Good note:
> Dense dashboard uses muted separators and card elevation sparingly, preserving scanability despite high information density.

Bad note:
> Super clean and awesome UI.

---

## 8. Pattern extraction template

For each strong example, ask:
1. What is the main reusable idea?
2. Is it visual, structural, or behavioral?
3. Where would it work well?
4. Why does it succeed here?
5. What are the tradeoffs?
6. Is it generic or distinctive?
7. Is it worth training on?

Pattern note template:

```md
## Pattern
## Core idea
## Where it works
## Why it works
## Failure modes
## Related examples
## Training value
```

---

## 9. Initial taxonomy for design languages

Starter set:
- restrained_productivity
- cinematic_marketing
- playful_consumer
- ai_native_workspace
- modern_fintech
- enterprise_dense
- editorial_minimal
- tactile_mobile
- developer_tooling_precision
- futuristic_premium
- bold_conversion_marketing

This should evolve over time.

---

## 10. Dataset inclusion policy

Include when:
- signal is strong
- UI is legible
- composition is coherent
- it teaches a pattern or style
- metadata can be reasonably captured

Exclude or demote when:
- too blurry
- too derivative
- contextless eye candy
- impossible to parse
- duplicated many times
- mostly branding with no UI lesson

---

## 11. Recommended first workflow

### Phase 1
- define schema
- create folder structure
- add 5–10 sources
- ingest first 20 examples

### Phase 2
- annotate and tag
- derive first 10 patterns
- define first 5 design languages

### Phase 3
- build retrieval/browse tooling
- cluster examples
- score training suitability
- start building a curated gold set

---

## 12. Suggested first sources

Start with:
- @DamnGoodUI
- similar UI curation accounts on X
- Mobbin
- curated SaaS galleries
- top product sites you already admire
- strong real apps, not just concept shots
