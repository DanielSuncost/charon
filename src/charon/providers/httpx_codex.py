"""Codex Responses API provider — talks to chatgpt.com/backend-api/codex/responses.

Codex OAuth tokens (from ChatGPT Plus/Pro subscriptions) do NOT work with
the standard api.openai.com endpoint. They use a different API at chatgpt.com
that requires JWT account ID extraction and special headers.

Based on pi-agent's openai-codex-responses.ts and Hermes's codex integration.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from charon.providers import Message, ModelInfo, StreamDelta, ToolCall

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

CODEX_BASE_URL = 'https://chatgpt.com/backend-api/codex/responses'
CODEX_TOKEN_URL = 'https://auth.openai.com/oauth/token'
CODEX_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
MAX_RETRIES = 2
BASE_DELAY_MS = 1500


def _extract_account_id(token: str) -> str:
    """Extract chatgpt_account_id from the JWT token."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError('Invalid JWT')
        # Add padding for base64
        payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        account_id = payload.get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
        if not account_id:
            raise ValueError('No account ID in token')
        return account_id
    except Exception as e:
        raise ValueError(f'Failed to extract accountId from Codex token: {e}') from e


def _convert_messages_to_input(messages: list[Message]) -> list[dict]:
    """Convert Charon messages to Responses API input format.

    The Responses API uses a flat item list, not nested content arrays.
    Each item is a top-level object with a 'type' field.
    """
    result = []
    id_map: dict[str, str] = {}  # maps original IDs to fc_ prefixed IDs
    for msg in messages:
        if msg.role == 'user':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            result.append({
                'type': 'message',
                'role': 'user',
                'content': [{'type': 'input_text', 'text': content}],
            })
        elif msg.role == 'assistant':
            # Text output as a message item
            if isinstance(msg.content, str) and msg.content:
                result.append({
                    'type': 'message',
                    'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': msg.content}],
                })
            # Function calls as separate top-level items
            for tc in msg.tool_calls:
                # Codex requires IDs starting with 'fc_'
                call_id = tc.id
                if not call_id.startswith('fc_'):
                    call_id = f'fc_{call_id}'
                    id_map[tc.id] = call_id
                result.append({
                    'type': 'function_call',
                    'id': call_id,
                    'call_id': call_id,
                    'name': tc.name,
                    'arguments': json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments),
                })
        elif msg.role == 'tool_result':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            # Map tool_call_id to the fc_ version if it was remapped
            call_id = msg.tool_call_id or ''
            call_id = id_map.get(call_id, call_id)
            if not call_id.startswith('fc_'):
                call_id = f'fc_{call_id}'
            result.append({
                'type': 'function_call_output',
                'call_id': call_id,
                'output': content,
            })
    return result


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    """Convert Charon tool defs to Responses API format."""
    if not tools:
        return None
    result = []
    for t in tools:
        result.append({
            'type': 'function',
            'name': t.get('name', ''),
            'description': t.get('description', ''),
            'parameters': t.get('input_schema', {}),
        })
    return result


class HttpxCodexProvider:
    """Codex Responses API provider using httpx."""

    def __init__(self, api_key: str, refresh_token: str | None = None,
                 auth_store_path: str | None = None, timeout: float = 300.0):
        self._api_key = api_key
        self._refresh_token = refresh_token
        self._auth_store_path = auth_store_path
        self._timeout = timeout
        self._account_id: str | None = None

    def _get_account_id(self) -> str:
        if not self._account_id:
            self._account_id = _extract_account_id(self._api_key)
        return self._account_id

    def _build_headers(self) -> dict[str, str]:
        account_id = self._get_account_id()
        return {
            'Authorization': f'Bearer {self._api_key}',
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
            'chatgpt-account-id': account_id,
            'originator': 'charon',
            'User-Agent': 'charon/0.1',
            'OpenAI-Beta': 'responses=experimental',
        }

    def _token_expires_soon(self, skew_seconds: int = 60) -> bool:
        try:
            parts = self._api_key.split('.')
            if len(parts) != 3:
                return False
            payload_b64 = parts[1] + '=' * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = int(payload.get('exp') or 0)
            return bool(exp and exp <= int(time.time()) + skew_seconds)
        except Exception as e:
            _diag('httpx_codex', 'JWT exp parse failed; assuming token not expiring, refresh skipped', error=e)
            return False

    def _save_token_data(self, token_data: dict[str, Any]) -> None:
        if not self._auth_store_path:
            return
        try:
            path = Path(self._auth_store_path)
            store = json.loads(path.read_text()) if path.exists() else {'version': 1, 'providers': {}}
            store.setdefault('providers', {})
            auth = store['providers'].setdefault('openai-codex', {'tokens': {}, 'auth_type': 'oauth'})
            tokens = auth.setdefault('tokens', {})
            if token_data.get('access_token'):
                tokens['access_token'] = token_data['access_token']
            if token_data.get('refresh_token'):
                tokens['refresh_token'] = token_data['refresh_token']
            if token_data.get('expires_in'):
                tokens['expires_in'] = token_data['expires_in']
            auth['last_login'] = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())
            auth['auth_type'] = auth.get('auth_type') or 'oauth'
            store['active_provider'] = 'openai-codex'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(store, indent=2))
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception as e:
            _diag('httpx_codex', 'failed to persist refreshed tokens to auth store', error=e)

    async def _refresh_access_token(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    CODEX_TOKEN_URL,
                    data={
                        'grant_type': 'refresh_token',
                        'client_id': CODEX_CLIENT_ID,
                        'refresh_token': self._refresh_token,
                    },
                )
            if resp.status_code != 200:
                _diag('httpx_codex', 'OAuth token refresh rejected',
                      status=resp.status_code, body=resp.text[:200])
                return False
            token_data = resp.json()
            access_token = str(token_data.get('access_token') or '').strip()
            if not access_token:
                _diag('httpx_codex', 'OAuth refresh returned no access_token')
                return False
            self._api_key = access_token
            if token_data.get('refresh_token'):
                self._refresh_token = str(token_data['refresh_token'])
            self._account_id = None
            self._save_token_data(token_data)
            return True
        except Exception as e:
            _diag('httpx_codex', 'OAuth token refresh raised', error=e)
            return False

    def _read_tokens_from_disk(self) -> None:
        """Pick up tokens another process may have just refreshed."""
        if not self._auth_store_path:
            return
        try:
            store = json.loads(Path(self._auth_store_path).read_text())
            tokens = store.get('providers', {}).get('openai-codex', {}).get('tokens', {})
            saved_access = str(tokens.get('access_token') or '').strip()
            saved_refresh = str(tokens.get('refresh_token') or '').strip()
            if saved_access and saved_access != self._api_key:
                self._api_key = saved_access
                self._account_id = None
            if saved_refresh:
                self._refresh_token = saved_refresh
        except Exception as e:
            _diag('httpx_codex', 'failed to re-read tokens from auth store', error=e)

    async def _ensure_fresh_token(self) -> bool:
        if not self._token_expires_soon():
            return True
        if not self._refresh_token:
            return False
        if self._auth_store_path:
            return await self._locked_refresh()
        return await self._refresh_access_token()

    async def _locked_refresh(self) -> bool:
        """Refresh under the shared cross-process OAuth lock (single-use tokens)."""
        from charon.providers.oauth_lock import locked_refresh
        return await locked_refresh(
            str(self._auth_store_path) + '.lock',
            read_from_disk=self._read_tokens_from_disk,
            is_fresh=lambda: not self._token_expires_soon(),
            do_refresh=self._refresh_access_token,
        )

    async def stream(
        self,
        messages: list[Message],
        model: ModelInfo,
        system_prompt: str,
        tools: list[dict] | None = None,
        thinking_level: str = 'off',
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamDelta]:
        if not self._api_key:
            yield StreamDelta(type='error', error='No Codex API key configured')
            return

        if not await self._ensure_fresh_token():
            yield StreamDelta(type='error', error='Codex token expired and refresh failed. Run /setup provider codex --force.')
            return

        # Build request body in Responses API format
        input_items = _convert_messages_to_input(messages)
        api_tools = _convert_tools(tools)

        body: dict[str, Any] = {
            'model': model.model_id,
            'instructions': system_prompt or '',
            'input': input_items,
            'store': False,
            'stream': True,
        }

        if api_tools:
            body['tools'] = api_tools
            body['tool_choice'] = 'auto'
            body['parallel_tool_calls'] = True

        # Reasoning config
        if thinking_level != 'off':
            effort_map = {'minimal': 'low', 'low': 'low', 'medium': 'medium', 'high': 'high'}
            effort = effort_map.get(thinking_level, 'medium')
            body['reasoning'] = {'effort': effort, 'summary': 'auto'}

        # Retry loop for transient errors
        for attempt in range(MAX_RETRIES + 1):
            try:
                headers = self._build_headers()
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    async with client.stream('POST', CODEX_BASE_URL, json=body, headers=headers) as response:
                        if response.status_code != 200:
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8', errors='replace')
                            try:
                                err_json = json.loads(error_text)
                                error_text = err_json.get('error', {}).get('message', error_text)
                            except Exception:
                                if '<html' in error_text.lower():
                                    error_text = f'HTTP {response.status_code}'

                            if response.status_code in (429, 502, 503) and attempt < MAX_RETRIES:
                                import asyncio
                                await asyncio.sleep(BASE_DELAY_MS / 1000 * (2 ** attempt))
                                continue

                            if response.status_code == 401 and self._refresh_token and attempt < MAX_RETRIES:
                                if await self._refresh_access_token():
                                    continue

                            yield StreamDelta(type='error', error=f'Codex HTTP {response.status_code}: {error_text[:200]}')
                            return

                        # Parse SSE stream
                        input_tokens = 0
                        output_tokens = 0
                        current_tool_calls: dict[str, dict] = {}

                        async for raw_line in response.aiter_lines():
                            line = raw_line.strip()
                            if not line or line.startswith(':'):
                                continue
                            if not line.startswith('data: '):
                                continue
                            data_str = line[6:]
                            if data_str == '[DONE]':
                                break

                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            event_type = event.get('type', '')

                            # Text output
                            if event_type == 'response.output_text.delta':
                                text = event.get('delta', '')
                                if text:
                                    yield StreamDelta(type='text', text=text)

                            # Function call
                            elif event_type == 'response.function_call_arguments.delta':
                                item_id = event.get('item_id', '')
                                delta = event.get('delta', '')
                                if item_id not in current_tool_calls:
                                    current_tool_calls[item_id] = {'id': item_id, 'name': '', 'args': ''}
                                current_tool_calls[item_id]['args'] += delta

                            elif event_type == 'response.output_item.added':
                                item = event.get('item', {})
                                if item.get('type') == 'function_call':
                                    item_id = item.get('id', '')
                                    current_tool_calls[item_id] = {
                                        'id': item.get('call_id', item_id),
                                        'name': item.get('name', ''),
                                        'args': '',
                                    }

                            elif event_type == 'response.output_item.done':
                                item = event.get('item', {})
                                if item.get('type') == 'function_call':
                                    item_id = item.get('id', '')
                                    tc_data = current_tool_calls.get(item_id, {})
                                    name = tc_data.get('name') or item.get('name', '')
                                    args_str = tc_data.get('args') or item.get('arguments', '{}')
                                    try:
                                        args = json.loads(args_str)
                                    except Exception:
                                        args = {'raw': args_str}
                                    call_id = tc_data.get('id') or item.get('call_id', item_id)
                                    yield StreamDelta(
                                        type='tool_call',
                                        tool_call=ToolCall(id=call_id, name=name, arguments=args),
                                    )

                            # Reasoning (thinking)
                            elif event_type == 'response.reasoning_summary_text.delta':
                                text = event.get('delta', '')
                                if text:
                                    yield StreamDelta(type='thinking', text=text)

                            # Response complete
                            elif event_type in ('response.completed', 'response.done'):
                                resp_obj = event.get('response', {})
                                usage = resp_obj.get('usage', {})
                                input_tokens = usage.get('input_tokens', 0)
                                output_tokens = usage.get('output_tokens', 0)

                        yield StreamDelta(
                            type='done',
                            text=json.dumps({
                                'usage': {
                                    'input_tokens': input_tokens,
                                    'output_tokens': output_tokens,
                                    'total_tokens': input_tokens + output_tokens,
                                },
                                'stop_reason': 'end_turn',
                            }),
                        )
                        return  # success

            except httpx.ConnectError as e:
                if attempt < MAX_RETRIES:
                    import asyncio
                    await asyncio.sleep(BASE_DELAY_MS / 1000 * (2 ** attempt))
                    continue
                yield StreamDelta(type='error', error=f'Connection failed: {e}')
            except httpx.TimeoutException:
                yield StreamDelta(type='error', error=f'Request timed out after {self._timeout}s')
            except Exception as e:
                yield StreamDelta(type='error', error=f'Codex error: {e}')
            return
