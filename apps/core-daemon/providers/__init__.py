"""LLM provider abstraction for Charon.

Each provider implements the same streaming interface so the conversation
engine doesn't care which backend is active.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamDelta:
    """A single chunk from the LLM stream."""
    type: str  # 'text', 'thinking', 'tool_call', 'done', 'error'
    text: str = ''
    tool_call: ToolCall | None = None
    error: str | None = None


@dataclass
class AssistantResponse:
    """Complete assistant response after streaming finishes."""
    content: str = ''
    thinking: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = 'end_turn'  # end_turn, tool_use, error, max_tokens
    error_message: str | None = None
    model: str = ''
    provider: str = ''


@dataclass
class Message:
    """Unified message format for the conversation."""
    role: str  # 'system', 'user', 'assistant', 'tool_result'
    content: str | list[dict] = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    thinking: str = ''
    usage: Usage | None = None
    timestamp: float = 0.0


@dataclass
class ModelInfo:
    provider: str
    model_id: str
    context_window: int = 200000
    supports_thinking: bool = False
    supports_images: bool = False


class Provider(Protocol):
    """Protocol that all LLM providers must implement."""

    async def stream(
        self,
        messages: list[Message],
        model: ModelInfo,
        system_prompt: str,
        tools: list[dict] | None = None,
        thinking_level: str = 'off',
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamDelta]:
        """Stream a response from the LLM.

        Yields StreamDelta chunks. The final chunk has type='done'.
        On error, yields a single chunk with type='error'.
        """
        ...


def get_provider(provider_name: str) -> Provider:
    """Get a provider instance by name.

    Prefers lightweight httpx-based providers when the heavy SDKs
    (openai, anthropic) are not installed.
    """
    name = provider_name.lower().strip()

    if name == 'anthropic':
        try:
            from .anthropic import AnthropicProvider
            return AnthropicProvider()
        except ImportError:
            # Fallback: use httpx provider against Anthropic's messages API
            # (not yet implemented — require the SDK for now)
            raise ValueError(
                'anthropic package not installed. Run: pip install anthropic'
            )

    if name in ('openai', 'openai-compatible'):
        try:
            from .openai_compat import OpenAICompatProvider
            return OpenAICompatProvider()
        except ImportError:
            # Fallback to httpx provider
            from .httpx_openai import HttpxOpenAIProvider
            return HttpxOpenAIProvider(
                base_url=os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
                api_key=os.environ.get('OPENAI_API_KEY', ''),
            )

    if name in ('local', 'lmstudio', 'ollama'):
        base_url = os.environ.get('CHARON_LOCAL_BASE_URL', 'http://127.0.0.1:1234/v1')
        api_key = os.environ.get('CHARON_LOCAL_API_KEY', 'not-needed')
        # Always use httpx provider for local — no SDK needed
        from .httpx_openai import HttpxOpenAIProvider
        return HttpxOpenAIProvider(base_url=base_url, api_key=api_key)

    raise ValueError(f"Unknown provider: {provider_name}")
