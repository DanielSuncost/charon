from __future__ import annotations

import os
from pathlib import Path

from backend.dashboard import _load_libris_index
from charon.libris import libris_runtime


def test_iter_jsonl_reuses_unchanged_parse_and_invalidates_changes(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / 'events.jsonl'
    path.write_text('{"seq": 1}\n', encoding='utf-8')
    cache_key = str(path.resolve())
    with libris_runtime._JSONL_CACHE_LOCK:
        libris_runtime._JSONL_CACHE.pop(cache_key, None)

    original_read_text = Path.read_text
    reads = 0

    def counted_read_text(self, *args, **kwargs):
        nonlocal reads
        if self == path:
            reads += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', counted_read_text)

    first = libris_runtime._iter_jsonl(path)
    first[0]['seq'] = 99
    second = libris_runtime._iter_jsonl(path)
    assert first == [{'seq': 99}]
    assert second == [{'seq': 1}]
    assert first is not second
    assert reads == 1

    with path.open('a', encoding='utf-8') as handle:
        handle.write('{"seq": 2}\n')
    assert libris_runtime._iter_jsonl(path) == [{'seq': 1}, {'seq': 2}]
    assert reads == 2

    replacement = tmp_path / 'replacement.jsonl'
    replacement.write_text('{"seq": 9}\n', encoding='utf-8')
    os.replace(replacement, path)
    assert libris_runtime._iter_jsonl(path) == [{'seq': 9}]
    assert reads == 3


def test_dashboard_uses_valid_maintained_libris_index(tmp_path, monkeypatch):
    operation = libris_runtime.init_operation(
        tmp_path,
        tmp_path,
        prompt='Use the maintained project index',
    )

    def unexpected_rebuild(*_args, **_kwargs):
        raise AssertionError('valid dashboard reads must not rebuild the index')

    monkeypatch.setattr(
        libris_runtime,
        'rebuild_project_index',
        unexpected_rebuild,
    )
    index = _load_libris_index(tmp_path, tmp_path)

    assert [row['operation_id'] for row in index['operations']] == [
        operation['operation_id'],
    ]


def test_dashboard_repairs_a_corrupt_libris_index(tmp_path, monkeypatch):
    index_path = libris_runtime.research_root(tmp_path, tmp_path) / 'index.json'
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text('{not json', encoding='utf-8')
    repaired = {'operations': [], 'topics': []}
    calls = []

    def rebuild(state_dir, project_root):
        calls.append((state_dir, project_root))
        return repaired

    monkeypatch.setattr(libris_runtime, 'rebuild_project_index', rebuild)

    assert _load_libris_index(tmp_path, tmp_path) is repaired
    assert calls == [(tmp_path, tmp_path)]
