#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_NAME = os.environ.get('CHARON_EMBED_MODEL', 'BAAI/bge-base-en-v1.5')
MODEL_DEVICE = os.environ.get('CHARON_EMBED_DEVICE', '').strip() or None
IDLE_TIMEOUT_SECS = max(15, int(os.environ.get('CHARON_EMBED_IDLE_SECS', '120') or '120'))

_model = None
_model_dim: int | None = None
_model_lock = threading.Lock()
_last_activity = time.monotonic()
_activity_lock = threading.Lock()


def _get_model():
    global _model, _model_dim
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            kwargs: dict[str, Any] = {}
            if MODEL_DEVICE:
                kwargs['device'] = MODEL_DEVICE
            _model = SentenceTransformer(MODEL_NAME, **kwargs)
            probe = _model.encode('dim probe', normalize_embeddings=True)
            _model_dim = len(probe)
        return _model


def get_dim() -> int:
    global _model_dim
    if _model_dim is None:
        _get_model()
    return int(_model_dim or 0)


def touch_activity() -> None:
    global _last_activity
    with _activity_lock:
        _last_activity = time.monotonic()


def idle_for_seconds() -> float:
    with _activity_lock:
        return max(0.0, time.monotonic() - _last_activity)


def embed(texts: list[str]) -> list[list[float]]:
    touch_activity()
    model = _get_model()
    arr = model.encode(texts, normalize_embeddings=True)
    return [e.tolist() for e in arr]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        return

    def do_GET(self):
        touch_activity()
        if self.path == '/health':
            self._send(200, {'ok': True, 'model': MODEL_NAME, 'device': MODEL_DEVICE or 'auto'})
            return
        if self.path == '/dim':
            self._send(200, {'dim': get_dim()})
            return
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        touch_activity()
        if self.path != '/embed':
            self._send(404, {'error': 'not found'})
            return
        length = int(self.headers.get('Content-Length', '0') or 0)
        body = self.rfile.read(length) if length > 0 else b'{}'
        try:
            payload = json.loads(body.decode('utf-8'))
            texts = payload.get('texts') or []
            if not isinstance(texts, list):
                raise ValueError('texts must be a list')
            texts = [str(t) for t in texts]
            self._send(200, {'embeddings': embed(texts)})
        except Exception as e:
            self._send(400, {'error': str(e)})


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--state-dir', required=True)
    ap.add_argument('--port', type=int, default=0)
    args = ap.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    meta_path = state_dir / 'embedding_worker.json'
    port = int(args.port or _find_free_port())

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)

    def _idle_watch() -> None:
        while True:
            time.sleep(5.0)
            if idle_for_seconds() >= IDLE_TIMEOUT_SECS:
                try:
                    server.shutdown()
                except Exception:
                    pass
                return

    threading.Thread(target=_idle_watch, daemon=True).start()

    meta = {
        'pid': os.getpid(),
        'host': '127.0.0.1',
        'port': port,
        'model': MODEL_NAME,
        'device': MODEL_DEVICE or 'auto',
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    try:
        server.serve_forever()
    finally:
        try:
            if meta_path.exists():
                meta_path.unlink()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
