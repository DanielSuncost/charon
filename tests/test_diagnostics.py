"""Tests for the TUI-safe diagnostics helper."""

from charon.infra import diagnostics


def test_record_and_read_roundtrip(tmp_path):
    diagnostics.record('memory_engine', 'vec0 unavailable', state_dir=tmp_path,
                       error=RuntimeError('no such module: vec0'), dim=768)
    rows = diagnostics.read_recent(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r['component'] == 'memory_engine'
    assert r['message'] == 'vec0 unavailable'
    assert r['error'] == 'RuntimeError: no such module: vec0'
    assert r['dim'] == 768
    assert r['ts'].endswith('Z')


def test_record_appends(tmp_path):
    diagnostics.record('a', 'one', state_dir=tmp_path)
    diagnostics.record('b', 'two', state_dir=tmp_path)
    rows = diagnostics.read_recent(tmp_path)
    assert [r['component'] for r in rows] == ['a', 'b']


def test_record_never_raises_on_bad_state_dir(tmp_path):
    # Point at a path whose parent is a file → mkdir would fail; must be swallowed.
    bad_parent = tmp_path / 'afile'
    bad_parent.write_text('x')
    # Should not raise.
    diagnostics.record('x', 'msg', state_dir=bad_parent / 'sub')


def test_record_writes_nothing_to_stdout_or_stderr(tmp_path, capsys):
    diagnostics.record('memory_engine', 'something degraded', state_dir=tmp_path,
                       error=ValueError('boom'))
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''


def test_read_recent_missing_file(tmp_path):
    assert diagnostics.read_recent(tmp_path) == []


def test_record_string_error(tmp_path):
    diagnostics.record('p', 'm', state_dir=tmp_path, error='plain text reason')
    rows = diagnostics.read_recent(tmp_path)
    assert rows[0]['error'] == 'plain text reason'
