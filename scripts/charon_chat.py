#!/usr/bin/env python3
"""Charon CLI chat — interactive coding agent session.

Usage:
    python scripts/charon_chat.py [--provider anthropic|openai|local] [--model MODEL_ID]
    python scripts/charon_chat.py -q "question" [--max-steps N] [--no-approval] [--profile gaia]

With -q/--query, runs one autonomous multi-step turn and prints the final
assistant text to stdout (tool activity goes to stderr). This is the entry
point benchmark harnesses drive.

Environment variables:
    ANTHROPIC_API_KEY    - API key for Anthropic
    OPENAI_API_KEY       - API key for OpenAI
    CHARON_LOCAL_BASE_URL - Base URL for local provider (default: http://127.0.0.1:1234/v1)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add src/ (charon package) to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from charon.providers import ModelInfo, get_provider  # noqa: E402 — import requires sys.path setup above
from charon.conversation.conversation_engine import ConversationEngine  # noqa: E402 — import requires sys.path setup above


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

# Providers resolved through provider_bridge (onboarding config + OAuth)
# rather than get_provider(). 'auto' means the configured default route.
BRIDGE_PROVIDERS = {'auto', 'codex', 'openai-codex', 'claude-code'}


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
                # End of stream; usage/turn stats in event.data are intentionally not printed.
                pass

        print()  # newline after response
        print()


async def one_shot(engine: ConversationEngine, query: str) -> int:
    """Run one autonomous multi-step turn.

    Assistant text goes to stdout; tool activity and errors go to stderr so a
    harness can parse the reply from stdout alone.
    """
    saw_error = False
    text_parts: list[str] = []
    usage = {
        'input_tokens': 0,
        'output_tokens': 0,
        'cache_read_tokens': 0,
        'cache_write_tokens': 0,
        'total_tokens': 0,
    }

    async for event in engine.submit(query):
        if event.type == 'text_delta':
            text = event.data.get('text', '')
            text_parts.append(text)
            print(text, end='', flush=True)
        elif event.type == 'tool_call':
            name = event.data.get('tool_name', '')
            args = event.data.get('arguments', {})
            print(f'[tool] {name} {str(args)[:200]}', file=sys.stderr, flush=True)
        elif event.type == 'tool_execution_end':
            if event.data.get('is_error'):
                first_line = event.data.get('content', '').splitlines()[:1]
                print(f'[tool-error] {first_line[0] if first_line else ""}', file=sys.stderr, flush=True)
        elif event.type == 'message_end':
            turn_usage = event.data.get('usage') or {}
            for field in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'):
                try:
                    usage[field] += max(0, int(turn_usage.get(field, 0) or 0))
                except (TypeError, ValueError):
                    continue
        elif event.type == 'turn_end':
            # Separate turns so a marker line stays on its own line
            if text_parts and not text_parts[-1].endswith('\n'):
                text_parts.append('\n')
                print(flush=True)
        elif event.type == 'error':
            saw_error = True
            print(f'[error] {event.data.get("error", "unknown error")}', file=sys.stderr, flush=True)

    if text_parts and not text_parts[-1].endswith('\n'):
        print(flush=True)
    usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
    print(json.dumps({'usage': usage}, separators=(',', ':')), file=sys.stderr, flush=True)
    return 1 if (saw_error and not ''.join(text_parts).strip()) else 0


def main():
    parser = argparse.ArgumentParser(description='Charon interactive chat')
    parser.add_argument('--provider', default='anthropic',
                        help='LLM provider: anthropic, openai, local, auto (configured default '
                             'route incl. OAuth), codex, claude-code (default: anthropic)')
    parser.add_argument('--model', default=None,
                        help='Model ID (default depends on provider)')
    parser.add_argument('--cwd', default='.',
                        help='Working directory for the agent (default: current dir)')
    parser.add_argument('--context-window', type=int, default=None,
                        help='Context window size (default depends on model)')
    parser.add_argument('--max-tokens', type=int, default=32768,
                        help='Max output tokens per response (default: 32768)')
    parser.add_argument('-q', '--query', default=None,
                        help='One-shot mode: run this query autonomously, print the reply, exit')
    parser.add_argument('--max-steps', type=int, default=None,
                        help='One-shot mode: max reasoning turns (default: engine default of 50)')
    parser.add_argument('--no-approval', action='store_true',
                        help='Skip all tool approval gates (sets CHARON_SKIP_APPROVAL=1)')
    parser.add_argument('--profile', default=None,
                        help='Benchmark system-prompt profile (e.g. gaia)')
    args = parser.parse_args()

    provider_name = args.provider

    if provider_name in BRIDGE_PROVIDERS:
        # Route through provider_bridge: honors onboarding config + OAuth
        # credentials (codex, claude-code). 'auto' uses the configured default.
        from charon.infra import config as charon_config
        from charon.providers.provider_bridge import ProviderRouteError, create_provider_and_model

        state = charon_config.state_dir() or (ROOT / '.charon_state')
        route = None
        if provider_name != 'auto':
            if not args.model:
                print(f'{RED}Error: --provider {provider_name} requires --model{RESET}', file=sys.stderr)
                sys.exit(2)
            route = {'provider': provider_name, 'model_id': args.model}
            if args.context_window:
                route['context_window'] = args.context_window
        try:
            provider, model, ready = create_provider_and_model(state, route_override=route)
        except ProviderRouteError as e:
            print(f'{RED}Error: {e}{RESET}', file=sys.stderr)
            sys.exit(2)
        if not ready:
            print(f'{YELLOW}Warning: provider not fully configured; using local fallback{RESET}', file=sys.stderr)
    else:
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

    if args.no_approval:
        os.environ['CHARON_SKIP_APPROVAL'] = '1'

    cwd = Path(args.cwd).resolve()

    system_prompt = ''
    if args.profile:
        from charon.evaluation.benchmark_profile import benchmark_system_prompt
        try:
            system_prompt = benchmark_system_prompt(args.profile, str(cwd))
        except ValueError as e:
            print(f'{RED}Error: {e}{RESET}', file=sys.stderr)
            sys.exit(2)

    engine_kwargs = {}
    if args.max_steps:
        engine_kwargs['max_turns'] = args.max_steps

    engine = ConversationEngine(
        provider=provider,
        model=model,
        project_root=cwd,
        max_tokens=args.max_tokens,
        system_prompt=system_prompt,
        **engine_kwargs,
    )

    if args.query is not None:
        sys.exit(asyncio.run(one_shot(engine, args.query)))

    asyncio.run(chat_loop(engine))


if __name__ == '__main__':
    main()
