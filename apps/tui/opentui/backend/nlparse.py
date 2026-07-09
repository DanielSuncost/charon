"""Natural-language schedule parsing (intervals, cron)."""
from __future__ import annotations

import re



def _parse_interval_phrase(text: str) -> int:
    s = str(text or '').strip().lower()
    if not s:
        return 0
    if re.search(r'\bevery\s+hour\b|\bhourly\b', s):
        return 3600
    if re.search(r'\bevery\s+day\b|\bdaily\b', s):
        return 86400
    if re.search(r'\bevery\s+minute\b', s):
        return 60
    m = re.search(r'\bevery\s+(\d+)\s*(minute|minutes|hour|hours|day|days)\b', s)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    if 'minute' in unit:
        return n * 60
    if 'hour' in unit:
        return n * 3600
    if 'day' in unit:
        return n * 86400
    return 0


def _natural_language_to_cron(text: str) -> str:
    s = str(text or '').strip().lower()
    if not s:
        return ''
    if 'every day at ' in s or 'daily at ' in s:
        m = re.search(r'(?:every day at|daily at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', s)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            meridiem = (m.group(3) or '').lower()
            if meridiem == 'pm' and hour < 12:
                hour += 12
            if meridiem == 'am' and hour == 12:
                hour = 0
            return f'{minute} {hour} * * *'
    if 'every weekday at ' in s:
        m = re.search(r'every weekday at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', s)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            meridiem = (m.group(3) or '').lower()
            if meridiem == 'pm' and hour < 12:
                hour += 12
            if meridiem == 'am' and hour == 12:
                hour = 0
            return f'{minute} {hour} * * 1-5'
    return ''
