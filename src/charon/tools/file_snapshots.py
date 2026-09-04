"""Content versions and atomic text replacement for file tools."""
from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections import OrderedDict
from pathlib import Path


_lock = threading.RLock()
_versions: OrderedDict[str, tuple[tuple[int, int, int], str]] = OrderedDict()
_MAX_ENTRIES = 256


def _identity(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, getattr(info, 'st_ino', 0)


def file_version(path: Path) -> str:
    """Return a cached SHA-256 version, invalidated by file identity changes."""
    resolved = path.resolve()
    key = str(resolved)
    identity = _identity(resolved)
    with _lock:
        cached = _versions.get(key)
        if cached and cached[0] == identity:
            _versions.move_to_end(key)
            return cached[1]

    digest = hashlib.sha256()
    with resolved.open('rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    value = digest.hexdigest()
    # Do not cache a digest if the file changed during hashing.
    if _identity(resolved) != identity:
        return file_version(resolved)
    with _lock:
        _versions[key] = (identity, value)
        _versions.move_to_end(key)
        while len(_versions) > _MAX_ENTRIES:
            _versions.popitem(last=False)
    return value


def invalidate_file_version(path: Path) -> None:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path.absolute())
    with _lock:
        _versions.pop(key, None)


def atomic_write_text(path: Path, content: str, *, encoding: str = 'utf-8') -> None:
    """Replace a text file atomically while preserving its permission bits."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        pass
    temp = target.with_name(
        f'.{target.name}.{os.getpid()}.{threading.get_ident()}.tmp'
    )
    try:
        with temp.open('w', encoding=encoding, newline='') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    invalidate_file_version(target)


__all__ = ['atomic_write_text', 'file_version', 'invalidate_file_version']
