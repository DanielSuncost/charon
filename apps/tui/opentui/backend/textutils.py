"""ANSI/text cleanup helpers for terminal output and saved messages."""
from __future__ import annotations

import re



def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text or '')


def _last_visible_line(text: str) -> str:
    cleaned = _strip_ansi(text).replace('\r', '\n')
    parts = [p.strip() for p in cleaned.split('\n') if p.strip()]
    return (parts[-1] if parts else '')[:240]


def _normalize_visible_text(text: str) -> str:
    return ' '.join(_strip_ansi(text).replace('\r', '\n').split()).strip().lower()


def _looks_like_runtime_footer_line(norm: str) -> bool:
    footer_patterns = [
        r'^context usage\b',
        r'^token usage\b',
        r'^usage\b',
        r'^context window\b',
        r'^remaining context\b',
        r'^input tokens\b',
        r'^output tokens\b',
        r'^cached tokens\b',
        r'^reasoning tokens\b',
        r'^total tokens\b',
        r'^latency\b',
        r'^throughput\b',
    ]
    return any(re.search(p, norm) for p in footer_patterns)


def _extract_meaningful_text(output: str, prompt: str = '') -> str:
    cleaned = _strip_ansi(output).replace('\r', '\n')
    prompt_norm = _normalize_visible_text(prompt)
    noise_patterns = [
        r'new message detected',
        r'interrupting',
        r'^\s*thinking\b',
        r'^\s*tool\b',
        r'^\s*bash\b',
        r'^\s*read\b',
        r'^\s*edit\b',
        r'^\s*write\b',
        r'^\s*grep\b',
        r'^\s*sed\b',
        r'^\s*python\b',
        r'^\s*press\s+',
        r'^\s*waiting\s+',
        r'^\s*loading\s+',
        r'^\s*role:\s*(teacher|student)\b',
        r'^\s*(teacher|student)\s+message:\s*$',
        r'^\s*topic:\s+',
        r'type a message',
        r'ctrl\+c',
        r'lm\s*studio',
        r'openai compatible server',
        r'serving model',
        r'^\s*model\s*[:=]',
        r'\b\d+(?:\.\d+)?k/\d+(?:\.\d+)?k\b',
        r'\b\d+(?:\.\d+)?%\b',
        r'\b\d+(?:\.\d+)?s\b',
    ]
    lines = []
    saw_content = False
    for raw in cleaned.split('\n'):
        line = raw.strip()
        if not line:
            continue
        norm = ' '.join(line.split()).strip().lower()
        if not norm:
            continue
        if prompt_norm and (norm in prompt_norm or prompt_norm in norm):
            continue
        if any(re.search(p, norm) for p in noise_patterns):
            continue
        if line.startswith('❯') and ('role:' in norm or 'teacher message:' in norm or 'student message:' in norm):
            continue
        if saw_content and _looks_like_runtime_footer_line(norm):
            break
        if len(re.sub(r'[^A-Za-z0-9]+', '', line)) < 16:
            continue
        if re.fullmatch(r'[-─═━│┃┆┄┈\s]+', line):
            continue
        lines.append(line)
        saw_content = True
    return '\n'.join(lines)[-4000:]


def _clean_restored_text(text: str) -> str:
    """Strip leaked thinking tags from persisted assistant content."""
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'(?im)^\s*</?think>\s*$', '', text)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _sanitize_saved_messages(messages: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        item = dict(msg)
        if isinstance(item.get('content'), str):
            item['content'] = _clean_restored_text(item['content'])
        cleaned.append(item)
    return cleaned


def _iso_to_epoch(iso_str: str) -> float:
    """Convert an ISO-8601 timestamp to epoch seconds (best-effort)."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0
