"""OpenAI-compatible provider using httpx (no openai SDK needed).

Works with LM Studio, Ollama, vLLM, and any OpenAI-compatible endpoint.
Uses httpx for async streaming — the only dependency beyond stdlib.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator
from charon.infra import config


def _partial_tag_suffix_len(text: str, tag: str) -> int:
    """Length of the longest suffix of text that could be the start of tag."""
    max_len = min(len(text), len(tag) - 1)
    for size in range(max_len, 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


def _strip_orphan_think_text(text: str) -> str:
    """Remove leaked think tags from visible assistant text."""
    if not text:
        return text
    text = text.replace('<think>', '').replace('</think>', '')
    return text


def _drain_think_buffer(text_buffer: str, in_think_block: bool) -> tuple[list[StreamDelta], str, bool]:
    """Split visible text from inline <think> blocks, preserving partial tags across chunks."""
    out: list[StreamDelta] = []

    while text_buffer:
        if in_think_block:
            end_idx = text_buffer.find('</think>')
            if end_idx != -1:
                thinking_part = text_buffer[:end_idx]
                if thinking_part:
                    out.append(StreamDelta(type='thinking', text=thinking_part))
                in_think_block = False
                text_buffer = text_buffer[end_idx + len('</think>'):].lstrip('\n')
                continue

            keep = _partial_tag_suffix_len(text_buffer, '</think>')
            emit_upto = len(text_buffer) - keep
            if emit_upto <= 0:
                break
            out.append(StreamDelta(type='thinking', text=text_buffer[:emit_upto]))
            text_buffer = text_buffer[emit_upto:]
            continue

        start_idx = text_buffer.find('<think>')
        if start_idx != -1:
            if start_idx > 0:
                visible = _strip_orphan_think_text(text_buffer[:start_idx])
                if visible:
                    out.append(StreamDelta(type='text', text=visible))
            in_think_block = True
            text_buffer = text_buffer[start_idx + len('<think>'):]
            continue

        # Drop orphan closing tags even if we never saw the matching open tag.
        if text_buffer.startswith('</think>'):
            text_buffer = text_buffer[len('</think>'):].lstrip('\n')
            continue

        keep = _partial_tag_suffix_len(text_buffer, '<think>')
        emit_upto = len(text_buffer) - keep
        if emit_upto <= 0:
            break
        visible = _strip_orphan_think_text(text_buffer[:emit_upto])
        if visible:
            out.append(StreamDelta(type='text', text=visible))
        text_buffer = text_buffer[emit_upto:]

    return out, text_buffer, in_think_block

import httpx  # noqa: E402 — deliberate layout: pure helpers above, deps below

from charon.providers import Message, ModelInfo, StreamDelta, ToolCall  # noqa: E402
from charon.providers.http_client import AsyncClientPool  # noqa: E402
from charon.providers.http_errors import exception_error, http_error  # noqa: E402
from charon.providers.image_content import has_image_block, to_chat_completions_parts  # noqa: E402

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _try_lenient_json_repair(raw: str) -> dict | None:
    """Best-effort recovery for the single most common local-model tool-call
    failure: the completion got cut off (max_tokens, or the model just
    trailed off) mid-argument, so the JSON is truncated rather than wrong
    in some other way. Closes an unterminated string and pads the missing
    closing brackets/braces, in order, then retries the parse — never
    invents field values, only closes what's already open. Returns None
    (never raises) if the patched text still isn't a valid JSON object."""
    text = raw.strip()
    if not text:
        return None
    in_string = False
    escaped = False
    openers: list[str] = []
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[':
            openers.append(ch)
        elif ch in '}]' and openers:
            openers.pop()
    patched = text + ('"' if in_string else '')
    patched += ''.join('}' if ch == '{' else ']' for ch in reversed(openers))
    try:
        result = json.loads(patched)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


async def _repair_arguments_via_constrained_decoding(
    client: httpx.AsyncClient, base_url: str, headers: dict, model_id: str,
    tool_name: str, schema: dict, raw_arguments: str,
) -> dict | None:
    """Ask the same model to re-emit just this tool call's arguments,
    grammar-constrained to its schema (OpenAI-compatible response_format:
    json_schema — real, honored by LM Studio/llama.cpp's GBNF grammar,
    vLLM's guided decoding, and OpenAI's own Structured Outputs). This is
    the actual fix for output the truncation repair above can't recover
    from local models with unreliable native tool-calling. Returns None
    (never raises) if the server doesn't support constrained decoding, or
    the retry still doesn't parse as an object — callers keep today's {}
    fallback either way, so this can only make things better."""
    body = {
        'model': model_id,
        'messages': [
            {'role': 'system', 'content': 'Output ONLY a JSON object matching the given schema — no prose, no code fences.'},
            {'role': 'user', 'content': (
                f"Re-emit valid arguments for the tool `{tool_name}`. "
                f"The previous attempt was not valid JSON: {raw_arguments[:2000]}"
            )},
        ],
        'max_tokens': 2048,
        'stream': False,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': tool_name, 'schema': schema, 'strict': True},
        },
    }
    try:
        resp = await client.post(f'{base_url}/chat/completions', json=body, headers=headers, timeout=30.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '')
        result = json.loads(content) if content else None
    except Exception as e:
        _diag('httpx_openai', 'constrained-decoding tool-argument repair failed', error=e, tool_name=tool_name)
        return None
    return result if isinstance(result, dict) else None


async def _finalize_tool_call_arguments(
    raw_arguments: str, tool_name: str, schema: dict | None,
    *, client: httpx.AsyncClient, base_url: str, headers: dict, model_id: str,
) -> dict:
    """json.loads, then lenient truncation repair, then a grammar-
    constrained retry against the same server — in that order, each only
    attempted if the previous one failed. Never raises; {} is still the
    final fallback, exactly like before this existed."""
    if not raw_arguments:
        return {}
    try:
        return json.loads(raw_arguments)
    except json.JSONDecodeError:
        pass
    repaired = _try_lenient_json_repair(raw_arguments)
    if repaired is not None:
        _diag('httpx_openai', 'tool-call arguments recovered via truncation repair', tool_name=tool_name)
        return repaired
    if schema:
        constrained = await _repair_arguments_via_constrained_decoding(
            client, base_url, headers, model_id, tool_name, schema, raw_arguments,
        )
        if constrained is not None:
            _diag('httpx_openai', 'tool-call arguments recovered via constrained-decoding retry', tool_name=tool_name)
            return constrained
    _diag('httpx_openai', 'tool-call arguments unrecoverable; falling back to {}', tool_name=tool_name, raw_arguments=raw_arguments[:500])
    return {}


class HttpxOpenAIProvider:

    # Image blocks go out as Chat Completions image_url parts (image_content.py).
    supports_image_input = True
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
    ):
        self._base_url = (
            base_url or config.local_base_url() or config.DEFAULT_LOCAL_BASE_URL
        ).rstrip('/')
        self._api_key = api_key or config.local_api_key()
        self._timeout = timeout
        # Optional httpx handler for tests; when set, requests are served by
        # an in-process MockTransport instead of a real network connection.
        self._mock_handler = None
        self._clients = AsyncClientPool(timeout=self._timeout)
        self.last_request_metrics: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._clients.aclose()

    async def stream(
        self,
        messages: list[Message],
        model: ModelInfo,
        system_prompt: str,
        tools: list[dict] | None = None,
        thinking_level: str = 'off',
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamDelta]:

        api_messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
        ]
        api_messages.extend(_convert_messages(messages))

        body: dict[str, Any] = {
            'model': model.model_id,
            'messages': api_messages,
            'max_tokens': max_tokens,
            'stream': True,
            'stream_options': {'include_usage': True},
        }

        tool_schemas = _tool_schema_map(tools)
        if tools:
            openai_tools = _convert_tools(tools)
            if openai_tools:
                body['tools'] = openai_tools

        if thinking_level != 'off':
            effort_map = {'minimal': 'low', 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'high'}
            body['reasoning_effort'] = effort_map.get(str(thinking_level).strip().lower(), 'medium')

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self._api_key}',
        }

        url = f'{self._base_url}/chat/completions'

        transport = (
            httpx.MockTransport(self._mock_handler)
            if self._mock_handler is not None
            else None
        )

        try:
            client, reused = await self._clients.get(transport=transport)
            self.last_request_metrics = {
                'request_json_bytes': len(json.dumps(body, separators=(',', ':')).encode('utf-8')),
                'transport_reused': reused,
                'transport': 'http2' if getattr(client, 'http2', False) else 'http',
            }
            async with client.stream(
                    'POST', url,
                    json=body,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        error_text = error_body.decode('utf-8', errors='replace')
                        try:
                            err_json = json.loads(error_text)
                            error_text = err_json.get('error', {}).get('message', error_text)
                        except Exception:
                            # Strip HTML from error responses (502/503 from Cloudflare etc)
                            if '<html' in error_text.lower():
                                import re
                                title_match = re.search(r'<title>(.*?)</title>', error_text, re.IGNORECASE)
                                error_text = title_match.group(1) if title_match else f'HTTP {response.status_code}'
                        yield http_error(
                            error_text[:200],
                            status_code=response.status_code,
                            headers=response.headers,
                        )
                        return

                    current_tool_calls: dict[int, dict[str, str]] = {}
                    finish_reason: str | None = None
                    usage_data: dict[str, int] = {}
                    # Track <think> blocks that some models (Qwen, DeepSeek)
                    # emit inline rather than via a dedicated reasoning field.
                    in_think_block = False
                    text_buffer = ''

                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line == 'data: [DONE]':
                            break
                        if not line.startswith('data: '):
                            continue

                        json_str = line[6:]
                        try:
                            chunk = json.loads(json_str)
                        except json.JSONDecodeError:
                            continue

                        chunk_usage = chunk.get('usage') or {}
                        if chunk_usage:
                            usage_data = {
                                'input_tokens': int(chunk_usage.get('prompt_tokens', 0) or chunk_usage.get('input_tokens', 0) or 0),
                                'output_tokens': int(chunk_usage.get('completion_tokens', 0) or chunk_usage.get('output_tokens', 0) or 0),
                                'total_tokens': int(chunk_usage.get('total_tokens', 0) or 0),
                            }
                            if not usage_data['total_tokens']:
                                usage_data['total_tokens'] = usage_data['input_tokens'] + usage_data['output_tokens']

                        choices = chunk.get('choices', [])
                        if not choices:
                            continue

                        choice = choices[0]
                        delta = choice.get('delta', {})
                        finish_reason = choice.get('finish_reason') or finish_reason

                        # Text content — with inline <think> block detection
                        content = delta.get('content')
                        if content:
                            text_buffer += content
                            deltas, text_buffer, in_think_block = _drain_think_buffer(text_buffer, in_think_block)
                            for parsed in deltas:
                                yield parsed

                        # Reasoning / thinking (native field from some providers)
                        reasoning = delta.get('reasoning_content') or delta.get('reasoning')
                        if reasoning:
                            yield StreamDelta(type='thinking', text=reasoning)

                        # Tool calls (streamed incrementally)
                        tc_deltas = delta.get('tool_calls', [])
                        for tc in tc_deltas:
                            idx = tc.get('index', 0)
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {
                                    'id': tc.get('id', ''),
                                    'name': '',
                                    'arguments': '',
                                }
                            entry = current_tool_calls[idx]
                            if tc.get('id'):
                                entry['id'] = tc['id']
                            fn = tc.get('function', {})
                            if fn.get('name'):
                                entry['name'] = fn['name']
                            if fn.get('arguments'):
                                entry['arguments'] += fn['arguments']

                    # Flush any remaining buffered content, including partial tags at EOF.
                    if text_buffer:
                        if in_think_block:
                            yield StreamDelta(type='thinking', text=text_buffer)
                        else:
                            visible = _strip_orphan_think_text(text_buffer).strip()
                            if visible:
                                yield StreamDelta(type='text', text=visible)
                        text_buffer = ''

                    # Emit completed tool calls
                    for tc_data in current_tool_calls.values():
                        args = await _finalize_tool_call_arguments(
                            tc_data['arguments'], tc_data['name'], tool_schemas.get(tc_data['name']),
                            client=client, base_url=self._base_url, headers=headers, model_id=model.model_id,
                        )
                        yield StreamDelta(
                            type='tool_call',
                            tool_call=ToolCall(
                                id=tc_data['id'] or f'call_{uuid.uuid4().hex[:24]}',
                                name=tc_data['name'],
                                arguments=args,
                            ),
                        )

                    # Report usage accumulated from the streamed chunks above
                    # (OpenAI-style providers emit it on the final chunk when
                    # stream_options.include_usage is set).
                    yield StreamDelta(
                        type='done',
                        text=json.dumps({
                            'usage': usage_data,
                            'stop_reason': finish_reason or 'stop',
                        }),
                    )

        except httpx.ConnectError as e:
            delta = exception_error(e, prefix='Connection')
            delta.error = f'Connection failed to {self._base_url}: {e}. Is LM Studio / Ollama running?'
            yield delta
        except httpx.TimeoutException as e:
            delta = exception_error(e, prefix='Request')
            delta.error = f'Request timed out after {self._timeout}s'
            yield delta
        except Exception as e:
            yield exception_error(e)


def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert Charon messages to OpenAI chat format."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == 'user':
            if has_image_block(msg.content):
                content = to_chat_completions_parts(msg.content)
            else:
                content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            result.append({'role': 'user', 'content': content})

        elif msg.role == 'assistant':
            entry: dict[str, Any] = {'role': 'assistant'}
            content = msg.content if isinstance(msg.content, str) else ''
            if content:
                entry['content'] = content
            if msg.tool_calls:
                entry['tool_calls'] = [{
                    'id': tc.id,
                    'type': 'function',
                    'function': {
                        'name': tc.name,
                        'arguments': json.dumps(tc.arguments),
                    },
                } for tc in msg.tool_calls]
                # OpenAI requires content to be null or string when tool_calls present
                if not content:
                    entry['content'] = None
            result.append(entry)

        elif msg.role == 'tool_result':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            result.append({
                'role': 'tool',
                'tool_call_id': msg.tool_call_id or '',
                'content': content,
            })

    return result


def _tool_schema_map(tools: list[dict] | None) -> dict[str, dict]:
    """name -> JSON schema, accepting the same three tool-def shapes
    _convert_tools does — used to look up the right schema for a
    constrained-decoding argument repair once we only have the tool's name
    back from the streamed response."""
    mapping: dict[str, dict] = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if 'input_schema' in tool and tool.get('name'):
            mapping[tool['name']] = tool['input_schema']
        elif 'function' in tool:
            fn = tool.get('function') or {}
            if fn.get('name'):
                mapping[fn['name']] = fn.get('parameters') or {}
        elif 'parameters' in tool and tool.get('name'):
            mapping[tool['name']] = tool['parameters']
    return mapping


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Convert tool definitions to OpenAI function calling format."""
    result = []
    for tool in tools:
        if 'input_schema' in tool:
            # Anthropic-style → OpenAI-style
            result.append({
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('description', ''),
                    'parameters': tool['input_schema'],
                },
            })
        elif 'function' in tool:
            result.append(tool)
        elif 'parameters' in tool:
            result.append({
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('description', ''),
                    'parameters': tool['parameters'],
                },
            })
    return result
