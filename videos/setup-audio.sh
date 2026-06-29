#!/usr/bin/env bash
# Audio onboarding for the Charon reel — narration + music deps.
#   bash videos/setup-audio.sh            # narration (manim-voiceover + offline voice)
#   bash videos/setup-audio.sh --musicgen # also local AI music (MusicGen / audiocraft, heavy)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${CHARON_VENV:-$ROOT/.venv}/bin/python3"; [ -x "$PY" ] || PY="$(command -v python3)"
G="\033[0;32m"; Y="\033[0;33m"; N="\033[0m"

echo "Installing narration deps (manim-voiceover + offline TTS)..."
"$PY" -m pip install -q "manim-voiceover" pyttsx3 >/dev/null && echo -e "  ${G}+${N} manim-voiceover, pyttsx3"
# Optional: SoX improves silence trimming (ffmpeg covers the rest).
if command -v brew >/dev/null 2>&1 && ! command -v sox >/dev/null 2>&1; then
  brew install sox >/dev/null 2>&1 && echo -e "  ${G}+${N} sox" || echo -e "  ${Y}!${N} sox optional (skipped)"
fi

case "${1:-}" in
  --musicgen)
    echo "Installing local AI music (audiocraft + torch — large)..."
    "$PY" -m pip install -q audiocraft && echo -e "  ${G}+${N} audiocraft (MusicGen)" \
      || echo -e "  ${Y}!${N} audiocraft install failed — use a library track in videos/audio/ instead" ;;
esac

echo ""
echo "Voice backend: local 'say' on macOS by default (offline, zero-setup)."
echo "  - High-quality local: set CHARON_MLX_AUDIO_CMD to your mlx-audio CLI."
echo "  - Cloud opt-in:       CHARON_VOICE_BACKEND=elevenlabs ELEVENLABS_API_KEY=... (or openai)."
echo "Music backend: drop a track in videos/audio/, or --musicgen (local), or wire Suno in videos/music.py."
echo -e "${G}Audio ready.${N} Render narrated clips, then: /reel stitch --music"
