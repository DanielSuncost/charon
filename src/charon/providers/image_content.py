"""Carry list-form message content across to the OpenAI-family wire formats.

Charon builds multimodal user content as Anthropic-style blocks — the shape the
Anthropic adapters already send unchanged:

    {'type': 'text', 'text': '...'}
    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': '...'}}

The OpenAI-family adapters used to json.dumps() such a list into a single text
part, so an image reached the model as base64 prose. These helpers translate
each block into the target API's own part instead. Callers only route content
here when it actually holds an image, so text-only payloads are unchanged.
"""
from __future__ import annotations

import json
from typing import Any


def has_image_block(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') == 'image' for block in content
    )


def image_url(block: dict) -> str | None:
    """An image block's source as a URL: a data: URL for base64, else the URL."""
    source = block.get('source')
    if not isinstance(source, dict):
        return None
    if source.get('type') == 'base64' and source.get('data'):
        media_type = source.get('media_type') or 'image/png'
        return f'data:{media_type};base64,{source["data"]}'
    if source.get('type') == 'url' and source.get('url'):
        return str(source['url'])
    return None


def _text(block: Any) -> str | None:
    if isinstance(block, dict) and block.get('type') == 'text':
        return str(block.get('text') or '')
    return None


def to_chat_completions_parts(blocks: list) -> list[dict]:
    """Blocks -> Chat Completions content parts (text / image_url)."""
    parts: list[dict] = []
    for block in blocks:
        text = _text(block)
        url = image_url(block) if isinstance(block, dict) and block.get('type') == 'image' else None
        if text is not None:
            parts.append({'type': 'text', 'text': text})
        elif url is not None:
            parts.append({'type': 'image_url', 'image_url': {'url': url}})
        else:
            parts.append({'type': 'text', 'text': json.dumps(block)})
    return parts


def to_responses_input_parts(blocks: list) -> list[dict]:
    """Blocks -> Responses API message content (input_text / input_image)."""
    parts: list[dict] = []
    for block in blocks:
        text = _text(block)
        url = image_url(block) if isinstance(block, dict) and block.get('type') == 'image' else None
        if text is not None:
            parts.append({'type': 'input_text', 'text': text})
        elif url is not None:
            parts.append({'type': 'input_image', 'image_url': url})
        else:
            parts.append({'type': 'input_text', 'text': json.dumps(block)})
    return parts
