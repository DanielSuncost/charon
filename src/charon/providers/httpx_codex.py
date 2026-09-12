"""Codex Responses API provider — talks to chatgpt.com/backend-api/codex/responses.

Codex OAuth tokens (from ChatGPT Plus/Pro subscriptions) do NOT work with
the standard api.openai.com endpoint. They use a different API at chatgpt.com
that requires JWT account ID extraction and special headers.

Based on pi-agent's openai-codex-responses.ts and Hermes's codex integration.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from charon.infra import config
from charon.providers import Message, ModelInfo, StreamDelta, ToolCall
from charon.providers.http_client import AsyncClientPool
from charon.providers.http_errors import exception_error, http_error
from charon.providers.image_content import has_image_block, to_responses_input_parts

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

CODEX_BASE_URL = 'https://chatgpt.com/backend-api/codex/responses'
CODEX_TOKEN_URL = 'https://auth.openai.com/oauth/token'
CODEX_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
MAX_AUTH_RETRIES = 1


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
    emitted_call_ids: set[str] = set()
    emitted_output_ids: set[str] = set()
    for msg in messages:
        if msg.role == 'user':
            if has_image_block(msg.content):
                parts = to_responses_input_parts(msg.content)
            else:
                content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
                parts = [{'type': 'input_text', 'text': content}]
            result.append({
                'type': 'message',
                'role': 'user',
                'content': parts,
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
                if not call_id:
                    continue
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
                emitted_call_ids.add(call_id)
        elif msg.role == 'tool_result':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            # Map tool_call_id to the fc_ version if it was remapped
            call_id = msg.tool_call_id or ''
            if not call_id:
                continue
            call_id = id_map.get(call_id, call_id)
            if not call_id.startswith('fc_'):
                call_id = f'fc_{call_id}'
            # Defense in depth: never send a function_call_output unless its
            # call has already been emitted in this request.  Compaction and
            # external callers must not be able to create a protocol-invalid
            # Codex payload.
            if call_id not in emitted_call_ids or call_id in emitted_output_ids:
                continue
            result.append({
                'type': 'function_call_output',
                'call_id': call_id,
                'output': content,
            })
            emitted_output_ids.add(call_id)
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


class _CodexResponseParser:
    """Translate raw Responses events for either SSE or WebSocket transport."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.current_tool_calls: dict[str, dict[str, str]] = {}
        self.emitted_tool_calls: set[str] = set()
        self.terminal = False
        self.response_id = ''

    def _done(self, stop_reason: str = 'end_turn') -> StreamDelta:
        self.terminal = True
        return StreamDelta(
            type='done',
            text=json.dumps({
                'usage': {
                    'input_tokens': self.input_tokens,
                    'output_tokens': self.output_tokens,
                    'cache_read_tokens': self.cached_tokens,
                    'total_tokens': self.input_tokens + self.output_tokens,
                },
                'stop_reason': stop_reason,
            }),
        )

    def feed(self, event: dict[str, Any]) -> list[StreamDelta]:
        event_type = str(event.get('type') or '')
        result: list[StreamDelta] = []

        response = event.get('response') or {}
        if isinstance(response, dict):
            response_id = response.get('id')
            if isinstance(response_id, str) and response_id:
                self.response_id = response_id

        if event_type == 'response.output_text.delta':
            text = str(event.get('delta') or '')
            if text:
                result.append(StreamDelta(type='text', text=text))

        elif event_type == 'response.function_call_arguments.delta':
            item_id = str(event.get('item_id') or '')
            entry = self.current_tool_calls.setdefault(
                item_id,
                {'id': item_id, 'name': '', 'args': ''},
            )
            entry['args'] += str(event.get('delta') or '')

        elif event_type == 'response.output_item.added':
            item = event.get('item') or {}
            if isinstance(item, dict) and item.get('type') == 'function_call':
                item_id = str(item.get('id') or '')
                self.current_tool_calls[item_id] = {
                    'id': str(item.get('call_id') or item_id),
                    'name': str(item.get('name') or ''),
                    'args': '',
                }

        elif event_type == 'response.output_item.done':
            item = event.get('item') or {}
            if isinstance(item, dict) and item.get('type') == 'function_call':
                item_id = str(item.get('id') or '')
                data = self.current_tool_calls.get(item_id, {})
                call_id = str(data.get('id') or item.get('call_id') or item_id)
                if call_id and call_id not in self.emitted_tool_calls:
                    args_text = str(data.get('args') or item.get('arguments') or '{}')
                    try:
                        arguments = json.loads(args_text)
                    except Exception:
                        arguments = {'raw': args_text}
                    result.append(StreamDelta(
                        type='tool_call',
                        tool_call=ToolCall(
                            id=call_id,
                            name=str(data.get('name') or item.get('name') or ''),
                            arguments=arguments,
                        ),
                    ))
                    self.emitted_tool_calls.add(call_id)

        elif event_type == 'response.reasoning_summary_text.delta':
            text = str(event.get('delta') or '')
            if text:
                result.append(StreamDelta(type='thinking', text=text))

        elif event_type in {'response.completed', 'response.done'}:
            usage = response.get('usage') if isinstance(response, dict) else {}
            usage = usage or {}
            self.input_tokens = int(usage.get('input_tokens', 0) or 0)
            self.output_tokens = int(usage.get('output_tokens', 0) or 0)
            details = usage.get('input_tokens_details') or {}
            self.cached_tokens = int(
                details.get('cached_tokens', 0)
                or usage.get('prompt_cache_hit_tokens', 0)
                or 0
            )
            result.append(self._done('end_turn'))

        elif event_type in {'response.failed', 'response.incomplete', 'error'}:
            error = event.get('error') or (
                response.get('error') if isinstance(response, dict) else None
            ) or {}
            if isinstance(error, dict):
                message = str(error.get('message') or error.get('code') or event_type)
                code = str(error.get('code') or event.get('code') or '')
            else:
                message = str(error)
                code = str(event.get('code') or '')
            self.terminal = True
            result.append(StreamDelta(
                type='error',
                error=f'Codex response error: {message}',
                error_code=code or 'response_error',
                retryable=(
                    code in {'server_error', 'internal_error', 'model_error'}
                    or event_type == 'response.incomplete'
                ),
            ))

        return result

    def finish(self) -> list[StreamDelta]:
        return [] if self.terminal else [self._done()]


class _CodexWebSocketTransportError(RuntimeError):
    pass


class HttpxCodexProvider:
    """Codex Responses API provider using httpx."""

    # Image blocks go out as Responses API input_image items (image_content.py).
    supports_image_input = True

    def __init__(self, api_key: str, refresh_token: str | None = None,
                 auth_store_path: str | None = None, timeout: float = 300.0):
        self._api_key = api_key
        self._refresh_token = refresh_token
        self._auth_store_path = auth_store_path
        self._timeout = timeout
        self._account_id: str | None = None
        self._clients = AsyncClientPool(timeout=self._timeout)
        self._session_id = ''
        self._prompt_cache_key = ''
        self.last_request_metrics: dict[str, Any] = {}
        self._websocket: Any = None
        self._websocket_loop: asyncio.AbstractEventLoop | None = None
        self._websocket_lock: asyncio.Lock | None = None
        self._websocket_last_activity = 0.0
        self._websocket_auth = ''
        self._websocket_disabled = False
        # Test seam: async callable(url, headers) -> socket-like object.
        self._websocket_factory = None

    def configure_session(
        self,
        session_id: str | None,
        prompt_cache_key: str | None = None,
    ) -> None:
        self._session_id = str(session_id or '')
        self._prompt_cache_key = str(prompt_cache_key or session_id or '')

    async def aclose(self) -> None:
        await self._close_websocket('provider-close')
        await self._clients.aclose()

    def _ensure_websocket_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._websocket_loop is not loop:
            self._websocket = None
            self._websocket_loop = loop
            self._websocket_lock = asyncio.Lock()
            self._websocket_last_activity = 0.0
            self._websocket_auth = ''
        assert self._websocket_lock is not None
        return self._websocket_lock

    @staticmethod
    def _websocket_is_open(socket: Any) -> bool:
        if socket is None:
            return False
        if bool(getattr(socket, 'closed', False)):
            return False
        state = getattr(socket, 'state', None)
        state_name = str(getattr(state, 'name', state) or '').upper()
        if state_name and state_name not in {'OPEN', '1'}:
            return False
        return getattr(socket, 'close_code', None) is None

    async def _close_websocket(self, reason: str = 'fallback') -> None:
        socket = self._websocket
        self._websocket = None
        self._websocket_last_activity = 0.0
        self._websocket_auth = ''
        if socket is None:
            return
        try:
            close = getattr(socket, 'close', None)
            if callable(close):
                result = close(code=1000, reason=reason[:120])
                if hasattr(result, '__await__'):
                    await result
        except Exception:
            pass

    async def _get_websocket(
        self,
        headers: dict[str, str],
    ) -> tuple[Any, bool]:
        loop = asyncio.get_running_loop()
        if self._websocket_loop is not loop:
            # A socket may only be used by its owning event loop. Short-lived
            # embedded runtimes get a fresh connection; the TUI reuses one loop.
            self._websocket = None
            self._websocket_loop = loop
            self._websocket_lock = asyncio.Lock()
            self._websocket_last_activity = 0.0
            self._websocket_auth = ''

        socket = self._websocket
        too_idle = (
            self._websocket_last_activity > 0
            and time.monotonic() - self._websocket_last_activity > 30.0
        )
        auth = headers.get('Authorization', '')
        if self._websocket_is_open(socket) and not too_idle and self._websocket_auth == auth:
            return socket, True
        if socket is not None:
            await self._close_websocket('stale-reuse')

        ws_headers = dict(headers)
        ws_headers.pop('Content-Type', None)
        ws_headers.pop('Accept', None)
        ws_headers['OpenAI-Beta'] = 'responses_websockets=2026-02-06'
        url = CODEX_BASE_URL.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
        if self._websocket_factory is not None:
            socket = await self._websocket_factory(url, ws_headers)
        else:
            import websockets
            socket = await websockets.connect(
                url,
                additional_headers=ws_headers,
                open_timeout=min(10.0, self._timeout),
                close_timeout=5.0,
                ping_interval=10.0,
                ping_timeout=60.0,
                max_size=None,
                max_queue=64,
            )
        self._websocket = socket
        self._websocket_auth = auth
        self._websocket_last_activity = time.monotonic()
        return socket, False

    async def prewarm(self) -> bool:
        """Establish the reusable Codex socket before the first user request."""
        if not config.codex_websocket() or self._websocket_disabled or not self._api_key:
            return False
        if not await self._ensure_fresh_token():
            return False
        lock = self._ensure_websocket_loop()
        try:
            async with lock:
                _, reused = await self._get_websocket(self._build_headers())
                self.last_request_metrics = {
                    'transport': 'websocket',
                    'transport_reused': reused,
                    'prewarmed': True,
                }
            return True
        except Exception as exc:
            self._websocket_disabled = True
            await self._close_websocket('prewarm-failure')
            _diag('httpx_codex', 'websocket prewarm failed; using SSE', error=exc)
            return False

    async def _stream_websocket(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[StreamDelta]:
        """Stream one full-context Responses WebSocket v2 request.

        The live socket is reused, but full protocol-safe context is sent each
        turn. This deliberately avoids fragile ``previous_response_id`` state:
        repaired/compacted tool transcripts can otherwise revive the orphaned
        call-id 400 that Charon must never emit again.
        """
        lock = self._ensure_websocket_loop()

        async with lock:
            try:
                socket, reused = await self._get_websocket(headers)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._close_websocket('connect-failure')
                raise _CodexWebSocketTransportError(str(exc)) from exc
            frame = {'type': 'response.create', **body}
            payload = json.dumps(frame, separators=(',', ':'))
            parser = _CodexResponseParser()
            self.last_request_metrics = {
                'request_json_bytes': len(payload.encode('utf-8')),
                'transport_reused': reused,
                'prompt_cache_key': bool(self._prompt_cache_key),
                'transport': 'websocket',
            }
            try:
                await socket.send(payload)
                first = True
                while True:
                    timeout = (
                        config.codex_websocket_first_event_timeout()
                        if first else self._timeout
                    )
                    raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
                    first = False
                    self._websocket_last_activity = time.monotonic()
                    if isinstance(raw, bytes):
                        raw = raw.decode('utf-8', errors='replace')
                    try:
                        event = json.loads(str(raw))
                    except json.JSONDecodeError as exc:
                        raise _CodexWebSocketTransportError(
                            f'invalid websocket JSON: {exc}'
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    for delta in parser.feed(event):
                        if delta.type == 'error':
                            raise _CodexWebSocketTransportError(
                                delta.error or 'Codex websocket response failed'
                            )
                        yield delta
                    if parser.terminal:
                        return
            except asyncio.CancelledError:
                await self._close_websocket('cancelled')
                raise
            except Exception as exc:
                await self._close_websocket('transport-failure')
                if isinstance(exc, _CodexWebSocketTransportError):
                    raise
                raise _CodexWebSocketTransportError(str(exc)) from exc

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
            # Fail closed: an unparseable JWT would 401 later anyway, so treat
            # it as expiring — the refresh attempt is the recoverable path.
            _diag('httpx_codex', 'JWT exp parse failed; treating token as expiring so a refresh is attempted', error=e)
            return True

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
            client, _ = await self._clients.get()
            resp = await client.post(
                CODEX_TOKEN_URL,
                data={
                    'grant_type': 'refresh_token',
                    'client_id': CODEX_CLIENT_ID,
                    'refresh_token': self._refresh_token,
                },
                timeout=30.0,
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
        if self._prompt_cache_key:
            body['prompt_cache_key'] = self._prompt_cache_key

        if api_tools:
            body['tools'] = api_tools
            body['tool_choice'] = 'auto'
            body['parallel_tool_calls'] = True

        # Reasoning config
        if thinking_level != 'off':
            effort_map = {'minimal': 'low', 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'high'}
            effort = effort_map.get(thinking_level, 'medium')
            body['reasoning'] = {'effort': effort, 'summary': 'auto'}

        # Codex WebSocket v2 avoids a new HTTP request/response setup on each
        # model turn. A failure before semantic output transparently falls back
        # to SSE; after output, replay would duplicate visible content and is
        # therefore surfaced as a structured transport error.
        websocket_fallback_error = ''
        if config.codex_websocket() and not self._websocket_disabled:
            emitted_semantic_output = False
            try:
                headers = self._build_headers()
                async for delta in self._stream_websocket(body, headers):
                    if delta.type in {'text', 'thinking', 'tool_call'}:
                        emitted_semantic_output = True
                    yield delta
                return
            except _CodexWebSocketTransportError as exc:
                self._websocket_disabled = True
                websocket_fallback_error = str(exc)[:200]
                fallback_metrics = dict(self.last_request_metrics)
                fallback_metrics.update({
                    'websocket_fallback': True,
                    'websocket_error': websocket_fallback_error,
                })
                self.last_request_metrics = fallback_metrics
                if emitted_semantic_output:
                    yield StreamDelta(
                        type='error',
                        error=f'Codex websocket transport error: {exc}',
                        error_code='websocket_error',
                        retryable=True,
                    )
                    return

        # Authentication refresh is provider-owned because it mutates the
        # credential. Transient transport/status retries are engine-owned.
        for attempt in range(MAX_AUTH_RETRIES + 1):
            try:
                headers = self._build_headers()
                client, reused = await self._clients.get()
                self.last_request_metrics = {
                    'request_json_bytes': len(json.dumps(body, separators=(',', ':')).encode('utf-8')),
                    'transport_reused': reused,
                    'prompt_cache_key': bool(self._prompt_cache_key),
                    'transport': 'sse',
                    **({
                        'websocket_fallback': True,
                        'websocket_error': websocket_fallback_error,
                    } if websocket_fallback_error else {}),
                }
                if client is not None:
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

                            if response.status_code == 401 and self._refresh_token and attempt < MAX_AUTH_RETRIES:
                                if await self._refresh_access_token():
                                    continue

                            yield http_error(
                                error_text[:200],
                                status_code=response.status_code,
                                headers=response.headers,
                                prefix='Codex HTTP',
                            )
                            return

                        parser = _CodexResponseParser()

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

                            for delta in parser.feed(event):
                                yield delta
                            if parser.terminal:
                                return

                        for delta in parser.finish():
                            yield delta
                        return  # success

            except httpx.ConnectError as e:
                delta = exception_error(e, prefix='Codex')
                delta.error = f'Connection failed: {e}'
                yield delta
            except httpx.TimeoutException as e:
                delta = exception_error(e, prefix='Codex')
                delta.error = f'Request timed out after {self._timeout}s'
                yield delta
            except Exception as e:
                yield exception_error(e, prefix='Codex')
            return
