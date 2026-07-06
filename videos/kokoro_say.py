"""Kokoro TTS with a fix for mlx-audio's SineGen length-mismatch bug.

mlx-audio 0.4.4 crashes on certain phoneme lengths with
`[broadcast_shapes] Shapes (1,N,1) and (1,M,9) cannot be broadcast` — its
`SineGen.__call__` builds the unvoiced/noise envelope (`uv`) and the sine
waveforms at time lengths that can differ by a few samples, then multiplies
them. We monkeypatch that one method to align both to their common length
before combining (a <1% trim, inaudible), then delegate to the normal
generator.

The upshot: every narration line synthesizes as ONE call, so Kokoro renders
its own natural, connected prosody instead of us chopping lines into stilted
clauses to dodge the crash.

    python videos/kokoro_say.py <text> <file_prefix>   # writes <file_prefix>_000.wav
"""
from __future__ import annotations

import sys

import mlx.core as mx
from mlx_audio.tts.models.kokoro import istftnet as _ist


def _patched_sinegen_call(self, f0: mx.array):
    fn = f0 * mx.arange(1, self.harmonic_num + 2)[None, None, :]
    sine_waves = self._f02sine(fn) * self.sine_amp
    uv = self._f02uv(f0)
    # align time dims — _f02sine can be off by a few samples vs _f02uv
    t = min(sine_waves.shape[1], uv.shape[1])
    sine_waves, uv = sine_waves[:, :t, :], uv[:, :t, :]
    noise_amp = uv * self.noise_std + (1 - uv) * self.sine_amp / 3
    noise = noise_amp * mx.random.normal(sine_waves.shape)
    sine_waves = sine_waves * uv + noise
    return sine_waves, uv, noise


_ist.SineGen.__call__ = _patched_sinegen_call


def say(text: str, file_prefix: str, *, model: str, voice: str) -> None:
    from mlx_audio.tts.generate import generate_audio
    generate_audio(text=text, model=model, voice=voice,
                   file_prefix=file_prefix, audio_format='wav', verbose=False)


if __name__ == '__main__':
    text, prefix = sys.argv[1], sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else 'mlx-community/Kokoro-82M-bf16'
    voice = sys.argv[4] if len(sys.argv) > 4 else 'bm_lewis'
    say(text, prefix, model=model, voice=voice)
