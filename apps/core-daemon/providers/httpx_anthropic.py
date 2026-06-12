"""Anthropic provider using httpx (no anthropic SDK needed).

Talks directly to https://api.anthropic.com/v1/messages.
Handles OAuth token refresh automatically before each request.

Key lesson: OAuth refresh tokens are SINGLE-USE. Each refresh returns
a new refresh token. We must save it immediately after each refresh.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from . import Message, ModelInfo, StreamDelta, ToolCall, Usage

try:
    from diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
ANTHROPIC_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
ANTHROPIC_VERSION = '2023-06-01'


class HttpxAnthropicProvider:
    def __init__(self, api_key: str | None = None, timeout: float = 300.0,
                 refresh_token: str | None = None,
                 auth_store_path: str | None = None):
        self._api_key = api_key or os.environ.get('ANTHROPIC_API_KEY', '')
        self._refresh_token = refresh_token
        self._timeout = timeout
        # Assume the token is valid for 8 hours from now if we have one
        # This prevents unnecessary refresh on every request
        self._token_expires: float = time.time() + 28800 - 300 if self._api_key else 0
        # Path to save updated tokens (so refresh tokens aren't lost)
        self._auth_store_path = auth_store_path

    def _save_tokens(self):
        """Persist updated tokens after refresh. Critical because refresh tokens are single-use.

        Saves to both Charon's auth store AND .claude/.credentials.json so that
        Claude Code's own refresh token doesn't get invalidated by our refresh.
        """
        # 1. Save to Charon's auth store
        if self._auth_store_path:
            try:
                path = Path(self._auth_store_path)
                if path.exists():
                    store = json.loads(path.read_text())
                    if 'anthropic' in store.get('providers', {}):
                        store['providers']['anthropic']['tokens']['access_token'] = self._api_key
                        if self._refresh_token:
                            store['providers']['anthropic']['tokens']['refresh_token'] = self._refresh_token
                        path.write_text(json.dumps(store, indent=2))
            except Exception:
                pass

        # 2. Sync to .claude/.credentials.json (keeps Claude Code working)
        try:
            cred_path = Path.home() / '.claude' / '.credentials.json'
            if cred_path.exists():
                cred = json.loads(cred_path.read_text())
                oauth = cred.get('claudeAiOauth', {})
                if oauth:
                    oauth['accessToken'] = self._api_key
                    if self._refresh_token:
                        oauth['refreshToken'] = self._refresh_token
                    if self._token_expires > 0:
                        # Convert to milliseconds epoch
                        oauth['expiresAt'] = int((self._token_expires + 300) * 1000)
                    cred_path.write_text(json.dumps(cred))
        except Exception:
            pass

    async def _refresh_if_needed(self):
        """Refresh OAuth token if expired. Must be called before every API request.

        Uses file locking (like pi-agent's proper-lockfile) to prevent race
        conditions when multiple Charon processes try to refresh simultaneously.
        The auth store file is the coordination point.
        """
        if not self._refresh_token or 'sk-ant-oat' not in self._api_key:
            return

        if self._token_expires > 0 and time.time() < self._token_expires:
            return

        # File-locked refresh (serializes across all Charon processes)
        if self._auth_store_path:
            await self._locked_refresh()
        else:
            await self._do_refresh()

    async def _locked_refresh(self):
        """Refresh with file lock — prevents multi-process races."""
        import fcntl

        lock_path = self._auth_store_path + '.lock'
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            lock_fd = open(lock_path, 'w')
            # Non-blocking attempt first, then blocking with timeout
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                # Another process holds the lock — wait up to 15 seconds
                import asyncio
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (IOError, OSError):
                        continue
                else:
                    lock_fd.close()
                    # Timed out — just read whatever's on disk
                    self._read_tokens_from_disk()
                    return

            try:
                # We have the lock. Read the latest state from disk.
                self._read_tokens_from_disk()

                # Check again — maybe another process refreshed while we waited
                if self._token_expires > 0 and time.time() < self._token_expires:
                    return

                # Still expired — do the actual refresh
                await self._do_refresh()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:
            # Lock failed entirely — try refreshing anyway
            self._read_tokens_from_disk()
            if self._token_expires > 0 and time.time() < self._token_expires:
                return
            await self._do_refresh()

    def _read_tokens_from_disk(self):
        """Read the latest tokens from the auth store file."""
        if not self._auth_store_path:
            return
        try:
            store = json.loads(Path(self._auth_store_path).read_text())
            tokens = store.get('providers', {}).get('anthropic', {}).get('tokens', {})
            saved_token = tokens.get('access_token', '').strip()
            saved_refresh = tokens.get('refresh_token', '').strip()
            if saved_token and saved_token != self._api_key:
                self._api_key = saved_token
                if saved_refresh:
                    self._refresh_token = saved_refresh
                self._token_expires = time.time() + 28800 - 300
        except Exception:
            pass

    async def _do_refresh(self):
        """Actually call Anthropic's token endpoint to refresh."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(ANTHROPIC_TOKEN_URL, json={
                    'grant_type': 'refresh_token',
                    'client_id': ANTHROPIC_CLIENT_ID,
                    'refresh_token': self._refresh_token,
                }, headers={'Accept': 'application/json'}, timeout=30.0)

                if resp.status_code == 200:
                    data = resp.json()
                    self._api_key = data.get('access_token', self._api_key)
                    if data.get('refresh_token'):
                        self._refresh_token = data['refresh_token']
                    expires_in = data.get('expires_in', 3600)
                    self._token_expires = time.time() + expires_in - 300
                    self._save_tokens()
                else:
                    _diag('httpx_anthropic', 'OAuth token refresh rejected',
                          status=resp.status_code, body=resp.text[:200])
        except Exception as e:
            _diag('httpx_anthropic', 'OAuth token refresh raised', error=e)

    async def stream(
        self,
        messages: list[Message],
        model: ModelInfo,
        system_prompt: str,
        tools: list[dict] | None = None,
        thinking_level: str = 'off',
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamDelta]:
        # Refresh token before every request
        await self._refresh_if_needed()

        if not self._api_key:
            yield StreamDelta(type='error', error='No Anthropic API key configured')
            return

        api_messages = _convert_messages(messages)

        # For OAuth tokens, we MUST include the Claude Code identity as the first
        # system message. Anthropic's server validates this for OAuth requests.
        is_oauth = 'sk-ant-oat' in self._api_key
        if is_oauth:
            system_content = [
                {'type': 'text', 'text': "You are Claude Code, Anthropic's official CLI for Claude."},
            ]
            if system_prompt:
                system_content.append({'type': 'text', 'text': system_prompt})
        else:
            system_content = system_prompt

        body: dict[str, Any] = {
            'model': model.model_id,
            'max_tokens': max_tokens,
            'system': system_content,
            'messages': api_messages,
            'stream': True,
        }

        if tools:
            body['tools'] = tools

        if thinking_level != 'off' and model.supports_thinking:
            budget = {'minimal': 1024, 'low': 4096, 'medium': 10000, 'high': 32000, 'xhigh': 100000}.get(thinking_level, 10000)
            body['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
            body['max_tokens'] = max(max_tokens, budget + 4096)

        # OAuth tokens use Bearer auth + special headers
        is_oauth = 'sk-ant-oat' in self._api_key
        headers: dict[str, str] = {
            'Content-Type': 'application/json',
            'anthropic-version': ANTHROPIC_VERSION,
            'accept': 'application/json',
        }
        if is_oauth:
            headers['Authorization'] = f'Bearer {self._api_key}'
            headers['anthropic-beta'] = 'claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14,interleaved-thinking-2025-05-14'
            headers['user-agent'] = 'claude-cli/2.1.80'
            headers['x-app'] = 'cli'
            headers['anthropic-dangerous-direct-browser-access'] = 'true'
        else:
            headers['x-api-key'] = self._api_key

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream('POST', ANTHROPIC_API_URL, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        error_text = error_body.decode('utf-8', errors='replace')
                        try:
                            err_json = json.loads(error_text)
                            error_text = err_json.get('error', {}).get('message', error_text)
                        except Exception:
                            pass

                        if response.status_code == 401 and self._refresh_token:
                            # Token rejected — re-read from disk (another process may have refreshed)
                            self._token_expires = 0
                            await self._refresh_if_needed()
                            yield StreamDelta(type='error', error='Token refreshed, retrying...')
                            return
                        elif response.status_code in (502, 503, 429):
                            if '<html' in error_text.lower():
                                import re
                                title_match = re.search(r'<title>(.*?)</title>', error_text, re.IGNORECASE)
                                error_text = title_match.group(1) if title_match else f'HTTP {response.status_code}'
                            yield StreamDelta(type='error', error=f'Anthropic HTTP {response.status_code}: {error_text[:200]}')
                        return

                    current_tool: dict[str, Any] | None = None
                    tool_input_json = ''
                    input_tokens = 0
                    output_tokens = 0

                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line or line.startswith(':'):
                            continue
                        if not line.startswith('data: '):
                            continue

                        json_str = line[6:]
                        if json_str == '[DONE]':
                            break

                        try:
                            event = json.loads(json_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get('type', '')

                        if event_type == 'message_start':
                            usage = event.get('message', {}).get('usage', {})
                            input_tokens = usage.get('input_tokens', 0)

                        elif event_type == 'content_block_start':
                            block = event.get('content_block', {})
                            if block.get('type') == 'tool_use':
                                current_tool = {'id': block.get('id', ''), 'name': block.get('name', '')}
                                tool_input_json = ''

                        elif event_type == 'content_block_delta':
                            delta = event.get('delta', {})
                            delta_type = delta.get('type', '')
                            if delta_type == 'text_delta':
                                yield StreamDelta(type='text', text=delta.get('text', ''))
                            elif delta_type == 'thinking_delta':
                                yield StreamDelta(type='thinking', text=delta.get('thinking', ''))
                            elif delta_type == 'input_json_delta':
                                tool_input_json += delta.get('partial_json', '')

                        elif event_type == 'content_block_stop':
                            if current_tool:
                                try:
                                    args = json.loads(tool_input_json) if tool_input_json else {}
                                except json.JSONDecodeError:
                                    args = {}
                                yield StreamDelta(
                                    type='tool_call',
                                    tool_call=ToolCall(
                                        id=current_tool['id'],
                                        name=current_tool['name'],
                                        arguments=args,
                                    ),
                                )
                                current_tool = None
                                tool_input_json = ''

                        elif event_type == 'message_delta':
                            usage = event.get('usage', {})
                            output_tokens = usage.get('output_tokens', 0)

                    yield StreamDelta(type='done', text=json.dumps({
                        'usage': {
                            'input_tokens': input_tokens,
                            'output_tokens': output_tokens,
                            'total_tokens': input_tokens + output_tokens,
                        },
                        'stop_reason': 'end_turn',
                    }))

        except httpx.ConnectError as e:
            yield StreamDelta(type='error', error=f'Connection failed to Anthropic API: {e}')
        except httpx.TimeoutException:
            yield StreamDelta(type='error', error=f'Request timed out after {self._timeout}s')
        except Exception as e:
            yield StreamDelta(type='error', error=f'Anthropic error: {e}')


def _convert_messages(messages: list[Message]) -> list[dict]:
    """Convert Charon messages to Anthropic API format."""
    result = []
    for msg in messages:
        if msg.role == 'user':
            if isinstance(msg.content, str):
                result.append({'role': 'user', 'content': msg.content})
            else:
                result.append({'role': 'user', 'content': msg.content})
        elif msg.role == 'assistant':
            content_blocks = []
            if msg.thinking:
                content_blocks.append({'type': 'thinking', 'thinking': msg.thinking})
            if isinstance(msg.content, str) and msg.content:
                content_blocks.append({'type': 'text', 'text': msg.content})
            for tc in msg.tool_calls:
                content_blocks.append({
                    'type': 'tool_use',
                    'id': tc.id,
                    'name': tc.name,
                    'input': tc.arguments,
                })
            if content_blocks:
                result.append({'role': 'assistant', 'content': content_blocks})
        elif msg.role == 'tool_result':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            result.append({
                'role': 'user',
                'content': [{
                    'type': 'tool_result',
                    'tool_use_id': msg.tool_call_id,
                    'content': content,
                    'is_error': msg.is_error,
                }],
            })
    return result
