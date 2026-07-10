from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from charon.infra import config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

ROOT = Path(__file__).resolve().parents[3]
WORKER_SCRIPT = ROOT / 'src' / 'charon' / 'memory' / 'embedding_worker.py'
MODEL_NAME = config.embed_model()
BACKEND = config.embed_backend()


def _backend() -> str:
    """Resolve the backend at call time so it can be overridden per-process
    (e.g. tests set CHARON_EMBED_BACKEND=local to avoid the worker subprocess)."""
    return config.embed_backend()


_LOCAL_MODEL = None


def _get_local_model():
    """Load the in-process SentenceTransformer once and cache it.

    The 'local' backend previously reloaded the model on every call, which made
    it far too slow to use in practice. Caching makes it a viable alternative to
    the worker subprocess (and removes per-call model-load latency in tests)."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        from sentence_transformers import SentenceTransformer
        _LOCAL_MODEL = SentenceTransformer(
            MODEL_NAME, device=config.embed_device()
        )
    return _LOCAL_MODEL


def _meta_path(state_dir: Path) -> Path:
    return state_dir / 'embedding_worker.json'


def _lock_path(state_dir: Path) -> Path:
    return state_dir / 'embedding_worker.lock'


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception as e:
        _diag('embedding_client', 'pid liveness check failed; treating worker as dead', error=e)
        return False


def _read_meta(state_dir: Path) -> dict[str, Any] | None:
    path = _meta_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except Exception as e:
        _diag('embedding_client', 'worker meta file unreadable; treating worker as absent', error=e)
        return None
    return None


def _request_json(url: str, data: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    req_data = None
    headers = {}
    if data is not None:
        req_data = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=req_data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _health_ok(meta: dict[str, Any]) -> bool:
    host = str(meta.get('host') or '127.0.0.1')
    port = int(meta.get('port') or 0)
    pid = int(meta.get('pid') or 0)
    if not port or not _is_pid_alive(pid):
        return False
    try:
        data = _request_json(f'http://{host}:{port}/health', timeout=1.0)
        if not bool(data.get('ok')):
            return False
        if str(data.get('model') or meta.get('model') or '') != MODEL_NAME:
            return False
        want_device = config.embed_device() or 'auto'
        got_device = str(data.get('device') or meta.get('device') or 'auto')
        return got_device == want_device
    except Exception:
        return False


def _acquire_lock(lock_path: Path, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.1)
    return False


def _release_lock(lock_path: Path) -> None:
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception as e:
        _diag('embedding_client', 'failed to remove worker lock file; next worker start may be delayed', error=e)


def ensure_worker(state_dir: Path) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    meta = _read_meta(state_dir)
    if meta and _health_ok(meta):
        return meta

    lock = _lock_path(state_dir)
    got_lock = _acquire_lock(lock)
    try:
        meta = _read_meta(state_dir)
        if meta and _health_ok(meta):
            return meta
        if meta:
            pid = int(meta.get('pid') or 0)
            if _is_pid_alive(pid):
                try:
                    os.kill(pid, 15)
                    time.sleep(0.2)
                except Exception as e:
                    _diag('embedding_client', 'failed to terminate stale embedding worker', error=e, pid=pid)

        env = os.environ.copy()
        cmd = ['python3', str(WORKER_SCRIPT), '--state-dir', str(state_dir)]
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        deadline = time.time() + 20.0
        while time.time() < deadline:
            meta = _read_meta(state_dir)
            if meta and _health_ok(meta):
                return meta
            time.sleep(0.2)
        raise RuntimeError('embedding worker failed to start')
    finally:
        if got_lock:
            _release_lock(lock)


def get_embedding_dim(state_dir: Path) -> int:
    if _backend() == 'local':
        model = _get_local_model()
        return len(model.encode('dim probe', normalize_embeddings=True))
    meta = ensure_worker(state_dir)
    host = str(meta.get('host') or '127.0.0.1')
    port = int(meta.get('port') or 0)
    data = _request_json(f'http://{host}:{port}/dim', timeout=10.0)
    return int(data['dim'])


def embed_texts(state_dir: Path, texts: list[str]) -> list[list[float]]:
    if _backend() == 'local':
        model = _get_local_model()
        arr = model.encode(texts, normalize_embeddings=True)
        return [e.tolist() for e in arr]
    meta = ensure_worker(state_dir)
    host = str(meta.get('host') or '127.0.0.1')
    port = int(meta.get('port') or 0)
    data = _request_json(f'http://{host}:{port}/embed', {'texts': texts}, timeout=120.0)
    return data['embeddings']
