"""Anthropic Claude provider for Charon."""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from charon.providers import Message, ModelInfo, StreamDelta, ToolCall, Usage

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


THINKING_BUDGET_MAP = {
    'minimal': 1024,
    'low': 4096,
    'medium': 10000,
    'high': 32000,
    'xhigh': 100000,
}


class AnthropicProvider:
    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get('ANTHROPIC_API_KEY', '')

    async def stream(
        self,
        messages: list[Message],
        model: ModelInfo,
        system_prompt: str,
        tools: list[dict] | None = None,
        thinking_level: str = 'off',
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamDelta]:
        if not HAS_ANTHROPIC:
            yield StreamDelta(type='error', error='anthropic package not installed. Run: pip install anthropic')
            return

        if not self._api_key:
            yield StreamDelta(type='error', error='ANTHROPIC_API_KEY not set')
            return

        client = anthropic.AsyncAnthropic(api_key=self._api_key)

        # Convert messages
        api_messages = _convert_messages(messages)

        kwargs: dict[str, Any] = {
            'model': model.model_id,
            'max_tokens': max_tokens,
            'system': system_prompt,
            'messages': api_messages,
        }

        if tools:
            kwargs['tools'] = tools

        if thinking_level != 'off' and model.supports_thinking:
            budget = THINKING_BUDGET_MAP.get(thinking_level, 10000)
            kwargs['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
            # Extended thinking requires higher max_tokens
            kwargs['max_tokens'] = max(max_tokens, budget + 4096)

        try:
            async with client.messages.stream(**kwargs) as stream:
                current_tool: dict[str, Any] | None = None
                tool_input_json = ''

                async for event in stream:
                    if event.type == 'content_block_start':
                        block = event.content_block
                        if block.type == 'thinking':
                            pass  # thinking content comes in deltas
                        elif block.type == 'tool_use':
                            current_tool = {'id': block.id, 'name': block.name}
                            tool_input_json = ''
                    elif event.type == 'content_block_delta':
                        delta = event.delta
                        if delta.type == 'text_delta':
                            yield StreamDelta(type='text', text=delta.text)
                        elif delta.type == 'thinking_delta':
                            yield StreamDelta(type='thinking', text=delta.thinking)
                        elif delta.type == 'input_json_delta':
                            tool_input_json += delta.partial_json
                    elif event.type == 'content_block_stop':
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

                # Get final message for usage
                final = await stream.get_final_message()
                usage = Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                    cache_read_tokens=getattr(final.usage, 'cache_read_input_tokens', 0) or 0,
                    cache_write_tokens=getattr(final.usage, 'cache_creation_input_tokens', 0) or 0,
                    total_tokens=final.usage.input_tokens + final.usage.output_tokens,
                )
                yield StreamDelta(type='done', text=json.dumps({
                    'usage': {
                        'input_tokens': usage.input_tokens,
                        'output_tokens': usage.output_tokens,
                        'total_tokens': usage.total_tokens,
                    },
                    'stop_reason': final.stop_reason,
                }))

        except anthropic.APIError as e:
            err_str = str(e)
            # Clean HTML from error messages (502/503 from Cloudflare)
            if '<html' in err_str.lower():
                import re
                title_match = re.search(r'<title>(.*?)</title>', err_str, re.IGNORECASE)
                err_str = f'Anthropic API: {title_match.group(1)}' if title_match else f'Anthropic API error (HTTP {getattr(e, "status_code", "?")})'
            yield StreamDelta(type='error', error=err_str[:200])
        except Exception as e:
            yield StreamDelta(type='error', error=f"Anthropic error: {str(e)[:200]}")


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
