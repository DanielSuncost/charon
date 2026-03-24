#!/usr/bin/env python3
from __future__ import annotations


def shorten_status(text: str, max_chars: int = 120) -> str:
    s = (text or '').strip()
    if not s:
        return ''
    for sep in ('. ', '! ', '? '):
        if sep in s:
            s = s.split(sep, 1)[0] + sep.strip()
            break
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + '…'


def format_assistant_message(text: str, message_type: str = 'status_update') -> str:
    if message_type == 'conversational':
        return (text or '').strip()
    if message_type == 'alert':
        return shorten_status(text, max_chars=100)
    return shorten_status(text, max_chars=120)
