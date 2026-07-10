#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / '.charon_state'
AUTH_DIR = STATE / 'auth'
AUTH_FILE = AUTH_DIR / 'auth.json'


@dataclass
class OAuthProvider:
    id: str
    name: str
    authorize_url: str
    token_url: str
    client_id: str
    scope: str
    redirect_uri: str
    flow: str = 'local_callback'  # local_callback | manual_code


PROVIDERS = {
    'openai-codex': OAuthProvider(
        id='openai-codex',
        name='ChatGPT Plus/Pro (Codex)',
        authorize_url='https://auth.openai.com/oauth/authorize',
        token_url='https://auth.openai.com/oauth/token',
        client_id='app_EMoamEEZ73f0CkXaXp7hrann',
        scope='openid profile email offline_access',
        redirect_uri='http://localhost:1455/auth/callback',
        flow='local_callback',
    ),
    'anthropic': OAuthProvider(
        id='anthropic',
        name='Anthropic (Claude Pro/Max)',
        authorize_url='https://claude.ai/oauth/authorize',
        token_url='https://platform.claude.com/v1/oauth/token',
        client_id='9d1c250a-e61b-44d9-88ed-5944d1962f5e',
        scope='org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload',
        redirect_uri='http://localhost:53692/callback',
        flow='local_callback',  # browser redirects back to local server, no manual code paste
    ),
}


SUCCESS_HTML = b"""<!doctype html><html><head><meta charset='utf-8'/><title>Auth successful</title></head><body><p>Authentication successful. Return to your terminal.</p></body></html>"""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode('utf-8')).digest())
    return verifier, challenge


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_auth() -> dict:
    if not AUTH_FILE.exists():
        return {'version': 1, 'providers': {}, 'active_provider': ''}
    try:
        return json.loads(AUTH_FILE.read_text())
    except Exception:
        return {'version': 1, 'providers': {}, 'active_provider': ''}


def _save_auth(store: dict) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(AUTH_DIR, 0o700)
    except Exception:
        pass
    AUTH_FILE.write_text(json.dumps(store, indent=2))
    try:
        os.chmod(AUTH_FILE, 0o600)
    except Exception:
        pass


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    code: Optional[str] = None
    state: Optional[str] = None
    done_event: threading.Event = None
    expected_path: str = '/callback'  # set before starting server

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Accept any path that matches the expected callback path
        if parsed.path != _OAuthCallbackHandler.expected_path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')
            return

        params = urllib.parse.parse_qs(parsed.query)

        # Check for error
        error = params.get('error', [None])[0]
        if error:
            self.send_response(400)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'<html><body><h1>Authentication Failed</h1><p>{error}</p></body></html>'.encode())
            return

        code = params.get('code', [None])[0]
        state = params.get('state', [None])[0]
        if code:
            _OAuthCallbackHandler.code = code
            _OAuthCallbackHandler.state = state
            if _OAuthCallbackHandler.done_event:
                _OAuthCallbackHandler.done_event.set()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(SUCCESS_HTML)


def _run_callback_server(host: str, port: int, timeout: int = 180, callback_path: str = '/callback') -> tuple[Optional[str], Optional[str]]:
    done = threading.Event()
    _OAuthCallbackHandler.code = None
    _OAuthCallbackHandler.state = None
    _OAuthCallbackHandler.done_event = done
    _OAuthCallbackHandler.expected_path = callback_path
    server = HTTPServer((host, port), _OAuthCallbackHandler)

    def _serve():
        while not done.is_set():
            server.handle_request()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    done.wait(timeout)
    server.server_close()
    return _OAuthCallbackHandler.code, _OAuthCallbackHandler.state


def _exchange_code_form(provider: OAuthProvider, code: str, verifier: str) -> dict:
    data = {
        'grant_type': 'authorization_code',
        'client_id': provider.client_id,
        'code_verifier': verifier,
        'code': code,
        'redirect_uri': provider.redirect_uri,
    }

    # Use httpx if available
    try:
        import httpx
        resp = httpx.post(
            provider.token_url,
            data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}')
        return resp.json()
    except ImportError:
        pass

    # Fallback: urllib
    body = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(
        provider.token_url,
        data=body,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Charon/1.0',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8')
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Token exchange failed (HTTP {e.code}): {error_body[:300]}') from e


def _exchange_code_json(provider: OAuthProvider, code: str, verifier: str, state: str | None = None) -> dict:
    payload = {
        'grant_type': 'authorization_code',
        'client_id': provider.client_id,
        'code': code,
        'redirect_uri': provider.redirect_uri,
        'code_verifier': verifier,
    }
    if state:
        payload['state'] = state

    # Use httpx if available (proper User-Agent, no Cloudflare blocking)
    # Fall back to urllib with a browser-like UA
    try:
        import httpx
        resp = httpx.post(
            provider.token_url,
            json=payload,
            headers={'Accept': 'application/json'},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}')
        return resp.json()
    except ImportError:
        pass

    # Fallback: urllib with proper User-Agent
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        provider.token_url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'Charon/1.0',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8')
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Token exchange failed (HTTP {e.code}): {error_body[:300]}') from e


def login_oauth(
    provider_id: str,
    status_cb: Optional[Callable[[str], None]] = None,
    auth_code_cb: Optional[Callable[[str], str]] = None,
) -> dict:
    provider = PROVIDERS.get(provider_id)
    if not provider:
        raise ValueError(f'unknown provider: {provider_id}')

    def emit(msg: str):
        if status_cb:
            status_cb(msg)

    verifier, challenge = _pkce_pair()
    # Use verifier as state (matches pi-agent's approach)
    state = verifier

    # Build the authorization URL
    auth_params = {
        'response_type': 'code',
        'client_id': provider.client_id,
        'redirect_uri': provider.redirect_uri,
        'scope': provider.scope,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
    }
    # Provider-specific extra params
    if provider.id == 'anthropic':
        auth_params['code'] = 'true'
    elif provider.id == 'openai-codex':
        auth_params['id_token_add_organizations'] = 'true'
        auth_params['codex_cli_simplified_flow'] = 'true'
        auth_params['originator'] = 'charon'

    auth_url = f"{provider.authorize_url}?{urllib.parse.urlencode(auth_params)}"

    if provider.flow == 'local_callback':
        # Start local callback server, open browser, wait for redirect
        parsed = urllib.parse.urlparse(provider.redirect_uri)
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port or 53692
        callback_path = parsed.path or '/callback'

        emit(f'AUTH_URL::{auth_url}')
        emit('AUTH_INFO::Complete login in your browser. If the browser is on another machine, paste the final redirect URL here.')
        emit('Waiting for OAuth callback...')

        code, returned_state = _run_callback_server(host, port, timeout=240, callback_path=callback_path)

        if not code and auth_code_cb:
            # Fallback: ask for manual code paste
            emit('AUTH_INFO::Browser redirect not received. Paste the authorization code or redirect URL:')
            raw = (auth_code_cb('Paste code or URL: ') or '').strip()
            if raw:
                # Parse — could be a URL, code#state, or just the code
                if '?' in raw:
                    parsed_url = urllib.parse.urlparse(raw)
                    params = urllib.parse.parse_qs(parsed_url.query)
                    code = (params.get('code') or [None])[0]
                    returned_state = (params.get('state') or [None])[0]
                elif '#' in raw:
                    parts = raw.split('#', 1)
                    code = parts[0].strip()
                    returned_state = parts[1].strip() if len(parts) > 1 else None
                else:
                    code = raw
                    returned_state = state

        if not code:
            raise RuntimeError('No authorization code received')

        emit('Exchanging code for tokens...')
        if provider.id == 'openai-codex':
            # Codex uses form-encoded token exchange
            token_data = _exchange_code_form(provider, code, verifier)
        else:
            # Anthropic uses JSON token exchange
            token_data = _exchange_code_json(provider, code, verifier, state=returned_state or state)
    else:
        # Manual code flow (for providers without local callback)
        emit(f'AUTH_URL::{auth_url}')
        emit('AUTH_INFO::After approving login, copy the full authorization code and paste it in the terminal.')
        if auth_code_cb is None:
            raise RuntimeError('OAuth code callback is required for manual_code flow')
        raw = (auth_code_cb('Paste authorization code: ') or '').strip()
        if not raw:
            raise RuntimeError('No authorization code provided')
        code_parts = raw.split('#', 1)
        query = code_parts[0].strip()
        params = urllib.parse.parse_qs(query)
        code = (params.get('code') or [query])[0]
        fragment = code_parts[1] if len(code_parts) > 1 else ''
        frag_params = urllib.parse.parse_qs(fragment)
        returned_state = frag_params.get('state', [None])[0]
        emit('Exchanging code for tokens...')
        token_data = _exchange_code_json(provider, code, verifier, state=returned_state)

    store = _load_auth()
    store['active_provider'] = provider.id
    store.setdefault('providers', {})
    store['providers'][provider.id] = {
        'tokens': token_data,
        'last_login': _now(),
        'auth_type': 'oauth',
    }
    _save_auth(store)
    emit('Tokens stored in .charon_state/auth/auth.json')
    token_data['auth_url'] = auth_url
    return token_data
