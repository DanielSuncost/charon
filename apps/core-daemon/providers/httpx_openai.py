"""OpenAI-compatible provider using httpx (no openai SDK needed).

Works with LM Studio, Ollama, vLLM, and any OpenAI-compatible endpoint.
Uses httpx for async streaming — the only dependency beyond stdlib.
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

from . import Message, ModelInfo, StreamDelta, ToolCall, Usage


class HttpxOpenAIProvider:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
    ):
        self._base_url = (base_url or os.environ.get(
            'CHARON_LOCAL_BASE_URL',
            'http://127.0.0.1:1234/v1',
        )).rstrip('/')
        self._api_key = api_key or os.environ.get('CHARON_LOCAL_API_KEY', 'not-needed')
        self._timeout = timeout

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
        }

        if tools:
            openai_tools = _convert_tools(tools)
            if openai_tools:
                body['tools'] = openai_tools

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self._api_key}',
        }

        url = f'{self._base_url}/chat/completions'

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
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
                        yield StreamDelta(
                            type='error',
                            error=f'HTTP {response.status_code}: {error_text[:200]}',
                        )
                        return

                    current_tool_calls: dict[int, dict[str, str]] = {}
                    finish_reason: str | None = None
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
                            # Process the buffer for <think> boundaries
                            while text_buffer:
                                if in_think_block:
                                    end_idx = text_buffer.find('</think>')
                                    if end_idx == -1:
                                        # Still inside think block, emit as thinking
                                        yield StreamDelta(type='thinking', text=text_buffer)
                                        text_buffer = ''
                                    else:
                                        # Emit thinking up to close tag, then switch back
                                        thinking_part = text_buffer[:end_idx]
                                        if thinking_part:
                                            yield StreamDelta(type='thinking', text=thinking_part)
                                        in_think_block = False
                                        text_buffer = text_buffer[end_idx + len('</think>'):]
                                        # Strip leading whitespace/newlines after think block
                                        text_buffer = text_buffer.lstrip('\n')
                                else:
                                    start_idx = text_buffer.find('<think>')
                                    if start_idx == -1:
                                        # No think tag, emit as text
                                        yield StreamDelta(type='text', text=text_buffer)
                                        text_buffer = ''
                                    elif start_idx == 0:
                                        # Think tag at start
                                        in_think_block = True
                                        text_buffer = text_buffer[len('<think>'):]
                                    else:
                                        # Text before think tag
                                        yield StreamDelta(type='text', text=text_buffer[:start_idx])
                                        in_think_block = True
                                        text_buffer = text_buffer[start_idx + len('<think>'):]

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

                    # Emit completed tool calls
                    for tc_data in current_tool_calls.values():
                        try:
                            args = json.loads(tc_data['arguments']) if tc_data['arguments'] else {}
                        except json.JSONDecodeError:
                            args = {}
                        yield StreamDelta(
                            type='tool_call',
                            tool_call=ToolCall(
                                id=tc_data['id'] or f'call_{id(tc_data)}',
                                name=tc_data['name'],
                                arguments=args,
                            ),
                        )

                    # Extract usage from final chunk if available
                    usage_data = {}
                    # Some providers put usage in the last chunk
                    # We just report what we have
                    yield StreamDelta(
                        type='done',
                        text=json.dumps({
                            'usage': usage_data,
                            'stop_reason': finish_reason or 'stop',
                        }),
                    )

        except httpx.ConnectError as e:
            yield StreamDelta(
                type='error',
                error=f'Connection failed to {self._base_url}: {e}. Is LM Studio / Ollama running?',
            )
        except httpx.TimeoutException:
            yield StreamDelta(
                type='error',
                error=f'Request timed out after {self._timeout}s',
            )
        except Exception as e:
            yield StreamDelta(type='error', error=f'Provider error: {e}')


def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert Charon messages to OpenAI chat format."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == 'user':
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
