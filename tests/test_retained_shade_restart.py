"""Retained shades are supposed to persist "across session boundaries", not
just across tool calls within one running process -- shade_lifecycle.py's
own module docstring says so explicitly. Every other test of reactivation
in this suite mocks ConversationEngine itself, which never exercises
load_from_store()'s real behavior; it only proves the call happened.

This test runs the real execute_spawn_shade -> _run_shade -> real
ConversationEngine path in one OS process, lets the shade go idle, kills
that process, and reactivates the SAME shade in a genuinely separate,
freshly-started process (fixtures/retained_shade_restart_phase{1,2}.py) that
shares nothing but the state_dir on disk -- the same boundary a real daemon
restart crosses. Only the model provider is faked; everything else
(ConversationEngine, the lossless context store, shade_lifecycle's FSM,
topology_budget) is the real production code.
"""
import json
import secrets
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / 'fixtures'
PHASE1 = FIXTURES / 'retained_shade_restart_phase1.py'
PHASE2 = FIXTURES / 'retained_shade_restart_phase2.py'


def _run(script: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f'{script.name} failed:\nstdout: {proc.stdout}\nstderr: {proc.stderr}'
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    assert lines, f'{script.name} produced no output:\nstderr: {proc.stderr}'
    return json.loads(lines[-1])


def test_retained_shade_context_survives_a_real_process_restart(tmp_path):
    state_dir = tmp_path / 'state'
    secret = f'CHECKPOINT-{secrets.token_hex(4).upper()}'

    # Process A: spawn a retained shade for real, let it go idle, exit.
    phase1 = _run(PHASE1, str(state_dir), secret)
    assert phase1['contract_status'] == 'completed'
    assert phase1['lifecycle_state'] == 'idle'
    shade_id = phase1['shade_id']

    # Process B: a fresh interpreter, sharing nothing with process A but
    # state_dir on disk -- reactivate the same shade.
    phase2 = _run(PHASE2, str(state_dir), shade_id, secret)
    assert phase2['state_before_reactivation'] == 'idle', (
        "process B could not see the idle state process A left on disk"
    )
    assert phase2['contract_status'] == 'completed'
    assert phase2['state_after_reactivation'] == 'idle'
    assert phase2['sent_message_count'] > 0
    assert phase2['secret_was_sent_to_model'] is True, (
        "the secret from process A's conversation was not present in the messages sent to "
        "the model in process B -- load_from_store() did not actually recover prior context "
        "across the process boundary"
    )
