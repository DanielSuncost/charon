"""OpenAI-compatible provider for Charon.

Works with OpenAI, LM Studio, Ollama, and any OpenAI-compatible API.
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from charon.providers import Message, ModelInfo, StreamDelta, ToolCall

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


class OpenAICompatProvider:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._base_url = base_url or os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
        self._api_key = api_key or os.environ.get('OPENAI_API_KEY', '')

    async def stream(
        self,
        messages: list[Message],
        model: ModelInfo,
        system_prompt: str,
        tools: list[dict] | None = None,
        thinking_level: str = 'off',
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamDelta]:
        if not HAS_OPENAI:
            yield StreamDelta(type='error', error='openai package not installed. Run: pip install openai')
            return

        client = openai.AsyncOpenAI(
            api_key=self._api_key or 'not-needed',
            base_url=self._base_url,
        )

        api_messages = [{'role': 'system', 'content': system_prompt}]
        api_messages.extend(_convert_messages(messages))

        kwargs: dict[str, Any] = {
            'model': model.model_id,
            'messages': api_messages,
            'max_tokens': max_tokens,
            'stream': True,
            'stream_options': {'include_usage': True},
        }

        if tools:
            # Convert from Anthropic tool format to OpenAI format
            openai_tools = _convert_tools(tools)
            if openai_tools:
                kwargs['tools'] = openai_tools

        try:
            stream = await client.chat.completions.create(**kwargs)

            current_tool_calls: dict[int, dict] = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Text content
                if delta.content:
                    yield StreamDelta(type='text', text=delta.content)

                # Tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                'id': tc.id or '',
                                'name': tc.function.name if tc.function and tc.function.name else '',
                                'arguments': '',
                            }
                        else:
                            if tc.id:
                                current_tool_calls[idx]['id'] = tc.id
                            if tc.function and tc.function.name:
                                current_tool_calls[idx]['name'] = tc.function.name
                        if tc.function and tc.function.arguments:
                            current_tool_calls[idx]['arguments'] += tc.function.arguments

                # Check for finish
                finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
                if finish_reason:
                    # Emit accumulated tool calls
                    for tc_data in current_tool_calls.values():
                        try:
                            args = json.loads(tc_data['arguments']) if tc_data['arguments'] else {}
                        except json.JSONDecodeError:
                            args = {}
                        yield StreamDelta(
                            type='tool_call',
                            tool_call=ToolCall(
                                id=tc_data['id'],
                                name=tc_data['name'],
                                arguments=args,
                            ),
                        )

                    # Extract usage from final chunk
                    usage_data = {}
                    if hasattr(chunk, 'usage') and chunk.usage:
                        usage_data = {
                            'input_tokens': chunk.usage.prompt_tokens or 0,
                            'output_tokens': chunk.usage.completion_tokens or 0,
                            'total_tokens': chunk.usage.total_tokens or 0,
                        }

                    yield StreamDelta(type='done', text=json.dumps({
                        'usage': usage_data,
                        'stop_reason': finish_reason,
                    }))

        except openai.APIError as e:
            yield StreamDelta(type='error', error=str(e))
        except Exception as e:
            yield StreamDelta(type='error', error=f"OpenAI-compatible error: {e}")


def _convert_messages(messages: list[Message]) -> list[dict]:
    """Convert Charon messages to OpenAI API format."""
    result = []
    for msg in messages:
        if msg.role == 'user':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            result.append({'role': 'user', 'content': content})
        elif msg.role == 'assistant':
            entry: dict[str, Any] = {'role': 'assistant'}
            if isinstance(msg.content, str):
                entry['content'] = msg.content
            if msg.tool_calls:
                entry['tool_calls'] = [{
                    'id': tc.id,
                    'type': 'function',
                    'function': {
                        'name': tc.name,
                        'arguments': json.dumps(tc.arguments),
                    },
                } for tc in msg.tool_calls]
            result.append(entry)
        elif msg.role == 'tool_result':
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            result.append({
                'role': 'tool',
                'tool_call_id': msg.tool_call_id,
                'content': content,
            })
    return result


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Convert from Anthropic-style tool definitions to OpenAI-style."""
    result = []
    for tool in tools:
        if 'input_schema' in tool:
            # Anthropic format
            result.append({
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('description', ''),
                    'parameters': tool['input_schema'],
                },
            })
        elif 'function' in tool:
            # Already OpenAI format
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
