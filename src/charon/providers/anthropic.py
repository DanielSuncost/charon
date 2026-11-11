"""Anthropic Claude provider for Charon."""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from charon.computer.destination import outgoing_capture_refusal
from charon.providers import Message, ModelInfo, StreamDelta, ToolCall
from charon.providers.httpx_anthropic import (
    _convert_messages as _convert_messages_shared,
    apply_prompt_caching,
    usage_from_anthropic,
)

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
    'max': 100000,
    'ultra': 100000,
}


class AnthropicProvider:
    # Anthropic-style image blocks are this API's native shape and pass through.
    supports_image_input = True

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
        # Desktop screenshots go only to the local model they were taken
        # for (charon.computer.destination); checked before any request.
        if refusal := outgoing_capture_refusal(messages, self, model, system_prompt):
            yield StreamDelta(type='error', error=refusal)
            return
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

        # Same breakpoints as the httpx provider. The top-level automatic
        # breakpoint goes through extra_body so SDK versions without the
        # parameter still send it.
        apply_prompt_caching(kwargs)
        kwargs['extra_body'] = {'cache_control': kwargs.pop('cache_control')}

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
                # The cache fields were read here before but never emitted.
                prompt_usage = usage_from_anthropic({
                    'input_tokens': final.usage.input_tokens,
                    'cache_read_input_tokens': getattr(final.usage, 'cache_read_input_tokens', 0),
                    'cache_creation_input_tokens': getattr(final.usage, 'cache_creation_input_tokens', 0),
                })
                yield StreamDelta(type='done', text=json.dumps({
                    'usage': {
                        **prompt_usage,
                        'output_tokens': final.usage.output_tokens,
                        'total_tokens': prompt_usage['input_tokens'] + final.usage.output_tokens,
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
    """Convert Charon messages to Anthropic API format (shared with httpx)."""
    return _convert_messages_shared(messages)
