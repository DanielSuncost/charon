# Installing Charon

## Development install from source

On a new machine:

```bash
git clone https://github.com/DanielSuncost/charon.git
cd charon
./scripts/install.sh
charon
```

This installs:
- system prerequisites on macOS / Ubuntu
- Python dependencies
- Playwright + Chromium (by default)
- the Rust TUI build
- a `charon` symlink in `~/.local/bin/charon`

## Requirements

Preferred path:
- `./scripts/install.sh` on macOS or Ubuntu

If you are using the lower-level project installer directly (`./scripts/install-dev.sh`), you need these already available on the machine:
- `python3`
- `cargo` / Rust toolchain

Optional but recommended:
- `uv`

If `~/.local/bin` is not on your `PATH`, either add it to your shell config or run Charon directly from the repo:

```bash
./charon
```

## Installer options

### Install (recommended)

```bash
./scripts/install.sh
```

### Skip browser support

```bash
./scripts/install.sh --no-playwright
```

### Force reinstall / rebuild

```bash
./scripts/install.sh --force
```

### Project-local installer only

```bash
./scripts/install-dev.sh
```

This lower-level installer assumes the machine already has Python, Rust, and other prerequisites available.

## x.com / browser features

Browser support is installed by default. That enables:
- x.com login
- bookmark checks
- browser-backed investigation flows

For first-time x.com login, it can help to launch Charon headful:

```bash
CHARON_BROWSER_HEADLESS=0 charon
```

Then ask Charon to open x.com login.

## Running Charon

After install:

```bash
charon
```

Other commands:

```bash
charon --resume
charon --agent AG-0005
charon --provider codex
charon chat
charon setup
charon status
charon agents
```

## Release / curlable install

Once GitHub release assets exist, you can also install without cloning the repo:

```bash
curl -fsSL https://raw.githubusercontent.com/DanielSuncost/charon/master/scripts/install-remote.sh | bash
```

That bootstrap script downloads and runs the release installer, which then:
- fetches the latest tagged release bundle from GitHub Releases
- installs it into `~/.charon/versions/<tag>`
- updates `~/.charon/current`
- symlinks `~/.local/bin/charon`
- installs Python dependencies and Playwright by default

You can also run the release installer directly from a checkout:

```bash
./scripts/install-release.sh
./scripts/install-release.sh --version v0.1.0
```

## Current status

Right now, the most reliable path is still the **source-based install** documented above.

That means:
- you install from a git checkout
- the Rust TUI is built locally
- dependencies are installed locally

The release installer path is now scaffolded, but depends on published GitHub Release assets for tagged versions.
