"""Narration for the Charon reel — time-synced voiceover, backend-pluggable.

Built on manim-voiceover: scenes wrap each beat in
    with self.voiceover(text="...") as tracker:
        self.play(anim, run_time=tracker.duration)
so the animation length follows the speech — per-scene time-sync, for free.

Backends (local default, cloud opt-in — matching the reel's audio choices):
  - say        : macOS `say` -> aiff -> mp3. Offline, zero-setup. Default on macOS.
  - mlx        : your mlx-audio (Qwen3-TTS/Orpheus) via a CLI shim. Highest-quality local.
                 Set CHARON_MLX_AUDIO_CMD="/path/to/cli --text {text} --out {out}".
  - elevenlabs : cloud, best quality (ELEVENLABS_API_KEY). manim-voiceover built-in.
  - openai     : cloud (OPENAI_API_KEY). manim-voiceover built-in.
  - gtts       : free cloud Google TTS (needs network at render).

Select with CHARON_VOICE_BACKEND; auto: mlx if CHARON_MLX_AUDIO_CMD set, else `say`
on macOS, else gtts.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from manim_voiceover import VoiceoverScene
from manim_voiceover.services.base import SpeechService
from manim_voiceover.helper import remove_bookmarks


def _ffmpeg() -> str:
    return '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'


class LocalSayService(SpeechService):
    """Offline macOS TTS via the built-in `say` command."""

    def __init__(self, voice: str | None = None, rate: int = 175, **kwargs):
        self.voice = voice or os.environ.get('CHARON_SAY_VOICE', 'Samantha')
        self.rate = rate
        super().__init__(**kwargs)

    def generate_from_text(self, text, cache_dir=None, path=None, **kwargs):
        if cache_dir is None:
            cache_dir = self.cache_dir
        input_text = remove_bookmarks(text)
        input_data = {'input_text': input_text, 'service': 'charon_say', 'voice': self.voice}
        cached = self.get_cached_result(input_data, cache_dir)
        if cached is not None:
            return cached
        audio_path = (self.get_audio_basename(input_data) + '.mp3') if path is None else str(path)
        aiff = Path(cache_dir) / (audio_path + '.aiff')
        subprocess.run(['say', '-v', self.voice, '-r', str(self.rate), '-o', str(aiff), input_text],
                       check=True)
        subprocess.run([_ffmpeg(), '-y', '-i', str(aiff), str(Path(cache_dir) / audio_path)],
                       check=True, capture_output=True)
        aiff.unlink(missing_ok=True)
        return {'input_text': text, 'input_data': input_data, 'original_audio': audio_path}


class MlxAudioService(SpeechService):
    """High-quality local TTS via an mlx-audio CLI shim (Apple Silicon).

    The command template comes from CHARON_MLX_AUDIO_CMD with {text}/{out}
    placeholders, e.g.:
        CHARON_MLX_AUDIO_CMD="mlx-audio-tts --voice orpheus --text {text} --out {out}"
    Must write a wav/mp3 to {out}.
    """

    def __init__(self, cmd_template: str | None = None, **kwargs):
        self.cmd_template = cmd_template or os.environ.get('CHARON_MLX_AUDIO_CMD', '')
        if not self.cmd_template:
            raise RuntimeError('Set CHARON_MLX_AUDIO_CMD to your mlx-audio CLI (with {text}/{out}).')
        super().__init__(**kwargs)

    def generate_from_text(self, text, cache_dir=None, path=None, **kwargs):
        if cache_dir is None:
            cache_dir = self.cache_dir
        input_text = remove_bookmarks(text)
        input_data = {'input_text': input_text, 'service': 'mlx_audio', 'cmd': self.cmd_template}
        cached = self.get_cached_result(input_data, cache_dir)
        if cached is not None:
            return cached
        audio_path = (self.get_audio_basename(input_data) + '.wav') if path is None else str(path)
        out = Path(cache_dir) / audio_path
        import shlex
        cmd = [a.replace('{text}', input_text).replace('{out}', str(out))
               for a in shlex.split(self.cmd_template)]
        subprocess.run(cmd, check=True)
        return {'input_text': text, 'input_data': input_data, 'original_audio': audio_path}


def make_speech_service(backend: str | None = None) -> SpeechService:
    backend = (backend or os.environ.get('CHARON_VOICE_BACKEND', '')).strip().lower()
    if not backend:
        if os.environ.get('CHARON_MLX_AUDIO_CMD'):
            backend = 'mlx'
        elif platform.system() == 'Darwin':
            backend = 'say'
        else:
            backend = 'gtts'

    if backend == 'say':
        return LocalSayService()
    if backend == 'mlx':
        return MlxAudioService()
    if backend == 'elevenlabs':
        from manim_voiceover.services.elevenlabs import ElevenLabsService
        return ElevenLabsService(voice_name=os.environ.get('CHARON_ELEVEN_VOICE', 'Alice'))
    if backend == 'openai':
        from manim_voiceover.services.openai import OpenAIService
        return OpenAIService(voice=os.environ.get('CHARON_OPENAI_VOICE', 'onyx'))
    if backend == 'gtts':
        from manim_voiceover.services.gtts import GTTSService
        return GTTSService()
    raise ValueError(f'Unknown voice backend: {backend}')


class CharonVoiceScene(VoiceoverScene):
    """Base scene with Charon's voice wired. Call setup_voice() first in construct()."""

    def setup_voice(self, backend: str | None = None) -> None:
        self.set_speech_service(make_speech_service(backend))
