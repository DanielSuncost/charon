# Changelog

Notable changes to Charon. Follows [Keep a Changelog](https://keepachangelog.com/) loosely; versions are milestones, not releases on a cadence.

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
