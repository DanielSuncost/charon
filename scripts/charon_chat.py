#!/usr/bin/env python3
"""Charon CLI chat — interactive coding agent session.

Usage:
    python scripts/charon_chat.py [--provider anthropic|openai|local] [--model MODEL_ID]

Environment variables:
    ANTHROPIC_API_KEY    - API key for Anthropic
    OPENAI_API_KEY       - API key for OpenAI
    CHARON_LOCAL_BASE_URL - Base URL for local provider (default: http://127.0.0.1:1234/v1)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add core-daemon to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from providers import ModelInfo, get_provider
from conversation_engine import ConversationEngine


# ANSI colors
CYAN = '\033[36m'
GREEN = '\033[32m'
YELLOW = '\033[33m'
DIM = '\033[2m'
BOLD = '\033[1m'
RESET = '\033[0m'
RED = '\033[31m'


DEFAULT_MODELS = {
    'anthropic': ('claude-sonnet-4-20250514', 200000),
    'openai': ('gpt-4o', 128000),
    'local': ('qwen3-30b-a3b', 65536),
}


async def chat_loop(engine: ConversationEngine):
    print(f'{BOLD}Charon Chat{RESET} — type your message, /reset to clear, /quit to exit')
    print(f'{DIM}Provider: {engine.model.provider} | Model: {engine.model.model_id} | CWD: {engine.project_root}{RESET}')
    print()

    while True:
        try:
            user_input = input(f'{GREEN}you>{RESET} ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nGoodbye.')
            break

        if not user_input:
            continue

        if user_input == '/quit':
            break
        if user_input == '/reset':
            engine.reset()
            print(f'{DIM}Conversation cleared.{RESET}')
            continue
        if user_input == '/messages':
            for i, m in enumerate(engine.messages):
                role = m.role
                content = m.content[:80] if isinstance(m.content, str) else str(m.content)[:80]
                print(f'{DIM}  [{i}] {role}: {content}...{RESET}')
            continue

        print(f'{CYAN}charon>{RESET} ', end='', flush=True)

        tool_depth = 0
        thinking_shown = False
        async for event in engine.submit(user_input):
            if event.type == 'thinking_delta':
                text = event.data.get('text', '')
                if not thinking_shown:
                    print(f'{DIM}  [thinking...]{RESET}', flush=True)
                    thinking_shown = True
                # Don't print thinking content — it's internal reasoning
                continue
            elif event.type == 'text_delta':
                if thinking_shown:
                    # First text after thinking — print the response header
                    print(f'{CYAN}charon>{RESET} ', end='', flush=True)
                    thinking_shown = False
                print(event.data.get('text', ''), end='', flush=True)
            elif event.type == 'tool_call':
                name = event.data.get('tool_name', '')
                args = event.data.get('arguments', {})
                # Format args concisely
                if name == 'Bash':
                    arg_str = args.get('command', '')
                elif name == 'Read':
                    arg_str = args.get('path', '')
                elif name == 'Write':
                    arg_str = f"{args.get('path', '')} ({len(args.get('content', ''))} chars)"
                elif name == 'Edit':
                    arg_str = args.get('path', '')
                else:
                    arg_str = str(args)[:60]
                print(f'\n{DIM}  ╭─ {name}({arg_str}){RESET}', flush=True)
                tool_depth += 1
            elif event.type == 'tool_execution_end':
                content = event.data.get('content', '')
                is_error = event.data.get('is_error', False)
                truncated = event.data.get('truncated', False)
                color = RED if is_error else DIM
                # Show first few lines of result
                lines = content.splitlines()[:5]
                for line in lines:
                    print(f'{color}  │ {line[:120]}{RESET}')
                if len(content.splitlines()) > 5:
                    print(f'{color}  │ ... ({len(content.splitlines())} lines total){RESET}')
                if truncated:
                    print(f'{color}  │ [truncated]{RESET}')
                print(f'{DIM}  ╰──{RESET}', flush=True)
                tool_depth -= 1
            elif event.type == 'turn_end':
                if event.data.get('stop_reason') == 'tool_use':
                    print(f'\n{CYAN}charon>{RESET} ', end='', flush=True)
            elif event.type == 'compaction_start':
                print(f'\n{YELLOW}  [compacting context...]{RESET}', flush=True)
            elif event.type == 'compaction_end':
                print(f'{YELLOW}  [context compacted]{RESET}', flush=True)
            elif event.type == 'error':
                error = event.data.get('error', 'unknown error')
                print(f'\n{RED}  Error: {error}{RESET}', flush=True)
            elif event.type == 'done':
                usage = event.data.get('usage', {})
                turns = event.data.get('total_turns', 0)
                msg_count = event.data.get('message_count', 0)
                if usage:
                    token_str = f"in={usage.get('input_tokens', '?')} out={usage.get('output_tokens', '?')}"
                else:
                    token_str = ''

        print()  # newline after response
        print()


def main():
    parser = argparse.ArgumentParser(description='Charon interactive chat')
    parser.add_argument('--provider', default='anthropic',
                        help='LLM provider: anthropic, openai, local (default: anthropic)')
    parser.add_argument('--model', default=None,
                        help='Model ID (default depends on provider)')
    parser.add_argument('--cwd', default='.',
                        help='Working directory for the agent (default: current dir)')
    parser.add_argument('--context-window', type=int, default=None,
                        help='Context window size (default depends on model)')
    parser.add_argument('--max-tokens', type=int, default=32768,
                        help='Max output tokens per response (default: 32768)')
    args = parser.parse_args()

    provider_name = args.provider
    model_defaults = DEFAULT_MODELS.get(provider_name, ('gpt-4o', 128000))
    model_id = args.model or model_defaults[0]
    context_window = args.context_window or model_defaults[1]

    model = ModelInfo(
        provider=provider_name,
        model_id=model_id,
        context_window=context_window,
        supports_thinking=(provider_name == 'anthropic'),
    )

    try:
        provider = get_provider(provider_name)
    except ValueError as e:
        print(f'{RED}Error: {e}{RESET}')
        sys.exit(1)

    cwd = Path(args.cwd).resolve()
    engine = ConversationEngine(
        provider=provider,
        model=model,
        project_root=cwd,
        max_tokens=args.max_tokens,
    )

    asyncio.run(chat_loop(engine))


if __name__ == '__main__':
    main()
