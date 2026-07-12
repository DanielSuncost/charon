"""Tests for charon.infra.fileio — quarantine-on-corrupt reads and atomic writes.

Also covers the three state files wired onto the helper: the task queue
(charon_loop), judge loop state (judge_engine), and — via the helper's own
semantics — the harvest adopted files.
"""
import json

from charon.infra.fileio import (
    read_json_or_quarantine,
    write_json_atomic,
)


# ── Helper semantics ────────────────────────────────────────────────

def test_read_missing_file_returns_default(tmp_path):
    assert read_json_or_quarantine(tmp_path / 'nope.json', [], component='t') == []


def test_read_valid_json(tmp_path):
    p = tmp_path / 'ok.json'
    p.write_text(json.dumps({'a': 1}))
    assert read_json_or_quarantine(p, {}, component='t') == {'a': 1}


def test_corrupt_file_is_quarantined_not_destroyed(tmp_path):
    p = tmp_path / 'state.json'
    p.write_text('{not json!!')

    result = read_json_or_quarantine(p, {'items': []}, component='t')
    assert result == {'items': []}

    # Original moved aside, byte-for-byte intact — a later rewrite of
    # state.json can no longer destroy it.
    assert not p.exists()
    quarantined = tmp_path / 'state.json.corrupt-0'
    assert quarantined.exists()
    assert quarantined.read_text() == '{not json!!'


def test_quarantine_counter_increments(tmp_path):
    p = tmp_path / 'state.json'
    p.write_text('bad one')
    read_json_or_quarantine(p, None, component='t')
    p.write_text('bad two')
    read_json_or_quarantine(p, None, component='t')

    assert (tmp_path / 'state.json.corrupt-0').read_text() == 'bad one'
    assert (tmp_path / 'state.json.corrupt-1').read_text() == 'bad two'


def test_write_json_atomic_roundtrip_and_no_temp_left(tmp_path):
    p = tmp_path / 'sub' / 'data.json'
    write_json_atomic(p, {'k': [1, 2]})
    assert json.loads(p.read_text()) == {'k': [1, 2]}
    # only the target file remains in its directory
    assert [f.name for f in p.parent.iterdir()] == ['data.json']

    write_json_atomic(p, {'k': 'ü'}, ensure_ascii=False)
    assert json.loads(p.read_text()) == {'k': 'ü'}


# ── Wired sites ─────────────────────────────────────────────────────

def test_load_queue_quarantines_corrupt_queue(tmp_path):
    from charon.charon_loop import load_queue, save_queue

    qf = tmp_path / 'queue.json'
    qf.write_text('[{"id": "T-1", truncated')

    assert load_queue(qf) == []
    quarantined = tmp_path / 'queue.json.corrupt-0'
    assert quarantined.exists()
    assert 'T-1' in quarantined.read_text()

    # The rewrite that previously destroyed the queue now only creates a
    # fresh file; the original content is still recoverable.
    save_queue(qf, [{'id': 'T-2'}])
    assert json.loads(qf.read_text()) == [{'id': 'T-2'}]
    assert 'T-1' in quarantined.read_text()


def test_judge_loops_corrupt_state_preserved_across_save(tmp_path):
    from charon.judge.judge_engine import JudgeLoopConfig, save_loop, load_loop

    loops_file = tmp_path / 'judge_loops.json'
    loops_file.write_text('{"id": "jl-old", oops')

    # save_loop loads (quarantining the corrupt file), then writes fresh state.
    save_loop(tmp_path, JudgeLoopConfig(id='jl-new', goal='g'))

    assert load_loop(tmp_path, 'jl-new') is not None
    quarantined = tmp_path / 'judge_loops.json.corrupt-0'
    assert quarantined.exists()
    assert 'jl-old' in quarantined.read_text()
