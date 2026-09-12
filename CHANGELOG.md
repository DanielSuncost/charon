# Changelog

Notable changes to Charon. Follows [Keep a Changelog](https://keepachangelog.com/) loosely; versions are milestones, not releases on a cadence.

## [Unreleased]

### Changed
- **Browser tool: stable element refs.** Interactive elements are tagged in-page with ids bound to the DOM node (`[e12]`, `[f1.e4]` inside a frame) instead of a position in the last listing, so a ref survives reflow, lazy-loaded rows and list mutation; a ref that outlives its element or document reports *why* it is stale instead of clicking the wrong thing. Ids are unique for the browser session. The legacy numeric `index` still works and is documented as positional.
- **Browser tool: every frame is walked.** Elements inside iframes — including cross-origin ones — are listed and clickable; `click_selector` / `input_selector` / `assert_selector` search all frames. Clicks are real Playwright pointer input (`isTrusted`), with a DOM-click fallback.
- **Browser tool: `get_state` is one round trip per frame and O(n)**, with a MutationObserver-backed snapshot cache. Measured on the same page and Chromium (`scripts/experiments/bench_browser_get_state.py`): 300 elements 11.3 → 4.3 ms, 2000 elements 139.9 → 4.3 ms, unchanged-DOM call 0.6 ms.

### Added
- **Browser tool: dialogs are answered by policy and reported.** `alert`/`confirm`/`prompt`/`beforeunload` never block; the default matches Playwright's previous silent behaviour (dismiss) but every dialog now appears in the next state with the policy that answered it, and `dialog_policy` switches to accept (with a prompt answer).
- **Browser tool: vision fallback.** When the DOM exposes no interactive elements (or a canvas covers ≥50% of the viewport) a screenshot is described by the configured provider into a fixed JSON shape, validated with `structured_output.schema_validation_errors`, and returned as coordinate refs `[v1]`… that `click` accepts; `vision` asks a question directly and `click_at` clicks coordinates. Only Anthropic-family adapters carry images today — the OpenAI-family adapters serialise list content as text — so the fallback degrades to an explicit message there. Disable with `CHARON_BROWSER_VISION=0`.
- Live-Chromium tests on inline local pages (`tests/test_browser_{refs,vision,dialogs_frames}.py`), skipped cleanly where no browser binary is installed.

## [0.2.0] — 2026-07-12

Large-scale restructuring for legibility, durability, and packaging honesty.

### Changed
- **`apps/core-daemon` is now the installable `src/charon` package** with 13 clustered subpackages (agents, conversation, context, memory, libris, judge, devop, shade, fleet, automation, providers, tools, infra). All `sys.path` hacks, importlib file-loading, and hyphenated-path workarounds are gone; `pip install -e .` works and CI proves it.
- **TUI backend split**: the 8,500-line `chat_backend.py` is now a 16-module `backend/` package; the slash-command router is a dispatch table over four command modules.
- **Rust TUI restructured**: `main.rs` split into `views/`, `cli`, `input`, and an `EventLoop` struct; zero compiler warnings; dead speculative code deleted.
- **Configuration centralized**: all 38 `CHARON_*` env vars have typed accessors in `charon.infra.config` and are documented in `src/charon/README.md`.
- Experiment scripts moved to `scripts/experiments/`; superseded planning docs archived under `docs/plans/archive/`.

### Fixed
- **Durability**: state files (task queue, judge loops, harvest records) are written atomically and quarantined — never overwritten — when unreadable; user-model saves are transactional; crashed batch workers reach a terminal `failed` state; auth token refresh fails closed on unparseable JWTs.
- Silent exception-swallowing across ~420 sites now records to the diagnostics sink (behavior unchanged, failures observable).
- Bugs unmasked by that audit: harbor voyage-result ingestion (TypeError since inception), fleet memory written to a nested `memory.db/memory.db`, daemon IndexError on the first successful autonomous self-assignment, `/harvest_souls` subcommand parsing off-by-one, plus three latent `NameError`/`UnboundLocalError` paths found by the new lint gate.
- Release bundles now include the full TUI backend package (previously broken since the backend split).
- Flaky automation-scheduler tests made deterministic against wall-clock stalls.

### Added
- Ruff lint gate (error-level rules) in CI alongside the test suite and Rust build.
- Regression tests for every bug fix above; suite grew from ~800 to 860+ tests.
- Architecture READMEs: `src/charon/README.md` (subsystem map, config table, error-handling policy) and `docs/README.md` (documentation index).

## [0.1.0] — 2026-04 to 2026-07

Initial development: specialist agents, three-tier memory (SQLite + local embeddings), shade swarms, judge loops with shadow-git checkpoints, session grid TUI (Rust), conversation rooms, Harbor remote dispatch, Libris research swarm, multi-provider support.
