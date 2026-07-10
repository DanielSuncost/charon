"""tmux capture/send mixin."""
from __future__ import annotations

from backend import common


class TmuxMixin:
    """tmux capture/send handlers and session-state detection."""

    def _detect_session_state(self, content: str) -> tuple[str, str]:
        """Heuristic: detect session state and generate summary from tmux content.
        Returns (state, summary).
        """
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            return 'idle', 'empty session'

        last_lines = lines[-5:]
        last_text = ' '.join(last_lines).lower()

        # Waiting for input?
        if any(p in last_text for p in ['[y/n]', '(y/n)', 'confirm', 'approve', 'continue?', 'proceed?']):
            return 'waiting', 'waiting for confirmation'
        if last_text.rstrip().endswith('?'):
            return 'waiting', 'question pending'

        # Error?
        if any(p in last_text for p in ['error:', 'failed', 'traceback', 'exception', 'panic']):
            return 'running', 'error detected'

        # At a prompt? (idle)
        last_line = lines[-1].strip() if lines else ''
        # Strip ANSI for pattern matching
        import re
        clean_last = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', last_line)
        if clean_last.endswith('$') or clean_last.endswith('❯') or clean_last.endswith('>') or clean_last.endswith('#'):
            return 'idle', 'at prompt'

        # Agent working patterns
        if any(p in last_text for p in ['thinking', 'reading', 'writing', 'editing', 'running', 'searching']):
            return 'running', 'working...'
        if any(p in last_text for p in ['tool_call', 'bash', 'executing']):
            return 'running', 'executing tools'
        if any(p in last_text for p in ['streaming', 'generating', '...']):
            return 'running', 'generating response'

        return 'running', 'active'

    # Cache for session state detection
    _session_states: dict[str, tuple[str, str]] = {}
    _session_hashes: dict[str, str] = {}

    def handle_tmux_capture(self, session_name: str, request_id: str | None):
        """Capture tmux pane content for the session grid."""
        try:
            from charon.fleet.tmux_capture import capture_pane
            content = capture_pane(session_name, width=120, height=40)

            # Detect state (only re-detect if content changed)
            import hashlib
            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
            if self._session_hashes.get(session_name) != content_hash:
                self._session_hashes[session_name] = content_hash
                state, summary = self._detect_session_state(content)
                self._session_states[session_name] = (state, summary)

            state, summary = self._session_states.get(session_name, ('idle', ''))

            common.emit({
                'type': 'tmux_capture',
                'session': session_name,
                'content': content,
                'state': state,
                'summary': summary,
                'request_id': request_id,
            })
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Capture failed: {e}', 'request_id': request_id})

    def handle_tmux_send(self, session_name: str, keys: str, literal: bool, request_id: str | None):
        """Send keys to a tmux session."""
        try:
            from charon.fleet.tmux_capture import send_keys, send_key_literal
            if literal:
                ok = send_key_literal(session_name, keys)
            else:
                ok = send_keys(session_name, keys)
            common.emit({
                'type': 'tmux_send_result',
                'session': session_name,
                'ok': ok,
                'request_id': request_id,
            })
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Send failed: {e}', 'request_id': request_id})
