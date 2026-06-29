# VHS tape template — records a REAL Charon TUI pane for the reel.
#
# Docs: https://github.com/charmbracelet/vhs   Run: vhs videos/tapes/<name>.tape
# The reel driver runs these via `python videos/reel.py capture <slug>` and the
# output is composited as the "real UI" body between the branded intro/outro.
#
# Copy this file to <feature-slug>.tape and script the keystrokes for that pane.
# Keep captures token-free where possible (command palette, /skills, /reel list,
# navigation) so re-recording is cheap and deterministic. For live-agent panes,
# pre-seed state or use a short scripted interaction.

# ── Output (the driver overrides Output to results/videos/<slug>/capture.mp4) ──
Output capture.mp4

# ── Brand-matched terminal styling (keep consistent across all tapes) ─────────
Set FontFamily "Menlo"
Set FontSize 22
Set Width 1600
Set Height 900
Set Padding 28
Set Margin 0
Set WindowBar Colorful          # macOS-style traffic lights, on brand
Set BorderRadius 12
Set Theme { "name": "charon", "background": "#12161C", "foreground": "#E8ECF1", "cursor": "#FFB020", "selection": "#22D3EE", "black": "#0A0A0A", "red": "#FF5F57", "green": "#7CE38B", "yellow": "#FFB020", "blue": "#22D3EE", "magenta": "#A78BFA", "cyan": "#22D3EE", "white": "#E8ECF1", "brightBlack": "#5A6472", "brightRed": "#FF5F57", "brightGreen": "#7CE38B", "brightYellow": "#FFB020", "brightBlue": "#22D3EE", "brightMagenta": "#A78BFA", "brightCyan": "#22D3EE", "brightWhite": "#FFFFFF" }
Set TypingSpeed 60ms

# ── Script (example: launch Charon, show a token-free pane) ───────────────────
# Replace the body with the interaction for this feature.
Hide
Type "cd ~/Projects/charon" Enter
Sleep 300ms
Show
Type "./charon"  Enter
Sleep 2s                         # let the TUI paint
Type "/reel list"  Enter         # token-free; swap for the feature's pane
Sleep 2500ms
Type "/skills"  Enter
Sleep 2500ms
# Exit cleanly so VHS finalizes the recording.
Ctrl+C
Sleep 500ms
