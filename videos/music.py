"""Music bed for the Charon reel — generate or supply a track, backend-pluggable.

Backends (local default, cloud opt-in — matching the reel's audio choices):
  - library : use a royalty-free file you drop in videos/audio/ (default, zero deps)
  - musicgen: local Meta MusicGen (audiocraft) — offline, prompt -> bed (heavy: torch)
  - suno    : cloud Suno API (SUNO_API_KEY) — prompt -> bed, check licensing

Select with CHARON_MUSIC_BACKEND, or it auto-picks: musicgen if audiocraft is
importable, else a library track if present, else none (reel stays silent-but-narrated).

    python videos/music.py "dark ambient synth, hopeful, minimal" 90 out.mp3
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = REPO_ROOT / 'videos' / 'audio'      # committed library tracks live here
DEFAULT_PROMPT = 'dark minimal ambient synth, hopeful, cinematic, steady pulse, no drums'


def _ffmpeg() -> str:
    return '/opt/homebrew/bin/ffmpeg' if Path('/opt/homebrew/bin/ffmpeg').exists() else 'ffmpeg'


def pick_backend() -> str:
    b = os.environ.get('CHARON_MUSIC_BACKEND', '').strip().lower()
    if b:
        return b
    try:
        import audiocraft  # noqa: F401
        return 'musicgen'
    except Exception:
        pass
    if AUDIO_DIR.is_dir() and any(AUDIO_DIR.glob('*.mp3')) or (AUDIO_DIR.is_dir() and any(AUDIO_DIR.glob('*.wav'))):
        return 'library'
    return 'none'


def _loop_to_length(src: Path, seconds: float, out: Path) -> Path:
    """Loop/trim a track to exactly `seconds` and fade in/out."""
    subprocess.run(
        [_ffmpeg(), '-y', '-stream_loop', '-1', '-i', str(src), '-t', f'{seconds:.2f}',
         '-af', f'afade=t=in:st=0:d=1.5,afade=t=out:st={max(0, seconds-2):.2f}:d=2',
         '-c:a', 'libmp3lame', '-q:a', '4', str(out)],
        check=True, capture_output=True)
    return out


def generate(prompt: str, seconds: float, out: Path, backend: str | None = None) -> Path:
    backend = backend or pick_backend()
    out.parent.mkdir(parents=True, exist_ok=True)

    if backend == 'library':
        tracks = sorted(AUDIO_DIR.glob('*.mp3')) + sorted(AUDIO_DIR.glob('*.wav'))
        if not tracks:
            raise FileNotFoundError(f'No library track in {AUDIO_DIR}. Drop a .mp3/.wav there.')
        return _loop_to_length(tracks[0], seconds, out)

    if backend == 'musicgen':
        try:
            import torch  # noqa: F401
            from audiocraft.models import MusicGen
            from audiocraft.data.audio import audio_write
        except Exception as e:
            raise RuntimeError(f'musicgen backend needs audiocraft+torch: {e}')
        model = MusicGen.get_pretrained('facebook/musicgen-small')
        model.set_generation_params(duration=min(seconds, 30))  # MusicGen caps ~30s
        wav = model.generate([prompt])[0]
        stem = out.with_suffix('')
        audio_write(str(stem), wav.cpu(), model.sample_rate, format='wav')
        return _loop_to_length(stem.with_suffix('.wav'), seconds, out)

    if backend == 'suno':
        key = os.environ.get('SUNO_API_KEY')
        if not key:
            raise RuntimeError('suno backend needs SUNO_API_KEY.')
        # Suno's API is async (submit -> poll). Kept as a documented stub: wire the
        # account's endpoint here, download the result, then _loop_to_length(...).
        raise NotImplementedError('Suno backend: add your account endpoint + poll in music.py.')

    raise RuntimeError(f'No music backend available (got "{backend}"). '
                       f'Set CHARON_MUSIC_BACKEND or drop a track in {AUDIO_DIR}.')


def _main(argv: list[str]) -> int:
    prompt = argv[0] if argv else DEFAULT_PROMPT
    seconds = float(argv[1]) if len(argv) > 1 else 60.0
    out = Path(argv[2]) if len(argv) > 2 else (REPO_ROOT / 'results' / 'videos' / 'music.mp3')
    print('backend:', pick_backend())
    print('wrote:', generate(prompt, seconds, out))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv[1:]))
