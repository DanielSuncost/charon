"""
assimilation.py — Scan sibling agent repos, extract capabilities, and generate
an abilities registry with gap analysis for Charon.

Usage (from chat_backend):
    from assimilation import run_full_assimilation, load_last_scan
"""

import asyncio
import ast
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ── Agent repo configs ───────────────────────────────────────────────

AGENT_REPOS: dict[str, dict] = {
    'hermes-agent': {
        'git_url': 'git@github.com:NousResearch/hermes-agent.git',
        'lang': 'python',
        'tools_dir': 'tools',
        'skills_dirs': ['skills', 'optional-skills'],
        'commands_file': 'hermes_cli/commands.py',
        'docs': ['README.md', 'AGENTS.md'],
    },
    'pi-mono': {
        'git_url': 'git@github.com:earendil-works/pi.git',
        'lang': 'typescript',
        'tools_dir': 'packages/coding-agent/src/tools',
        'skills_dirs': [],
        'commands_file': None,
        'docs': ['README.md', 'packages/coding-agent/CHANGELOG.md'],
    },
    'openclaw': {
        'git_url': 'git@github.com:openclaw/openclaw.git',
        'lang': 'typescript',
        'tools_dir': 'src/tools',
        'skills_dirs': ['skills', 'optional-skills'],
        'commands_file': None,
        'docs': ['README.md'],
    },
}


# ── Repo discovery ───────────────────────────────────────────────────

def _repo_path(name: str, state_dir: Path) -> Path:
    """Resolve the local path for an agent repo.

    First checks ~/Projects/<name> (user's own checkout).
    Falls back to .charon_state/assimilation/repos/<name> (our managed clone).
    """
    user_path = Path.home() / 'Projects' / name
    if user_path.is_dir():
        return user_path
    return state_dir / 'assimilation' / 'repos' / name


def scan_available_repos(
    state_dir: Path,
    agent_filter: str | None = None,
    auto_clone: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> tuple[dict[str, Path], list[str]]:
    """Return ({name: path}, [unavailable]) for agent repos.

    Checks ~/Projects/<name> first (user checkouts), then
    .charon_state/assimilation/repos/<name> (managed clones).
    If auto_clone is True, clones missing repos into the managed directory.
    Pulls latest for repos that already exist.
    """
    emit = on_status or (lambda msg: None)
    available = {}
    unavailable = []

    for name, config in AGENT_REPOS.items():
        if agent_filter and name != agent_filter:
            continue

        path = _repo_path(name, state_dir)
        git_url = config.get('git_url', '')

        if path.is_dir():
            # Pull latest
            if (path / '.git').is_dir():
                emit(f'Fetching latest for {name}...')
                try:
                    subprocess.run(
                        ['git', '-C', str(path), 'pull', '--ff-only', '--quiet'],
                        capture_output=True, timeout=60,
                    )
                except Exception:
                    pass  # Non-fatal — use whatever's there
            available[name] = path
        elif auto_clone and git_url:
            # Clone into managed directory
            clone_dir = state_dir / 'assimilation' / 'repos' / name
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            emit(f'Cloning {name}...')
            try:
                result = subprocess.run(
                    ['git', 'clone', '--depth=1', git_url, str(clone_dir)],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0 and clone_dir.is_dir():
                    emit(f'Cloned {name} successfully')
                    available[name] = clone_dir
                else:
                    stderr = result.stderr.strip()[:200]
                    emit(f'Failed to clone {name}: {stderr}')
                    unavailable.append(name)
            except Exception as e:
                emit(f'Failed to clone {name}: {e}')
                unavailable.append(name)
        else:
            unavailable.append(name)

    return available, unavailable


# ── Hermes tool scanner ──────────────────────────────────────────────

def _scan_hermes_tools(repo: Path) -> list[dict]:
    """Extract tool registrations from hermes-agent/tools/*.py."""
    tools_dir = repo / 'tools'
    if not tools_dir.is_dir():
        return []

    tools = []
    for py_file in sorted(tools_dir.glob('*.py')):
        if py_file.name.startswith('__') or py_file.name in (
            'registry.py', 'binary_extensions.py', 'ansi_strip.py',
            'fuzzy_match.py', 'debug_helpers.py', 'env_passthrough.py',
            'credential_files.py', 'path_security.py', 'url_safety.py',
            'website_policy.py', 'tool_result_storage.py', 'tool_backend_helpers.py',
            'interrupt.py', 'budget_config.py',
        ):
            continue

        try:
            text = py_file.read_text(errors='replace')
        except Exception:
            continue

        # Extract module docstring
        docstring = ''
        try:
            tree = ast.parse(text)
            docstring = ast.get_docstring(tree) or ''
        except SyntaxError:
            pass

        # Find registry.register() calls via regex — more robust than AST for kwargs
        for m in re.finditer(
            r'registry\.register\(\s*\n?\s*name\s*=\s*["\']([^"\']+)["\']'
            r'.*?toolset\s*=\s*["\']([^"\']+)["\']',
            text, re.DOTALL
        ):
            tool_name = m.group(1)
            toolset = m.group(2)

            # Try to extract description from schema or register() call
            desc = ''
            # Look for description= in the register() call block
            block_start = m.start()
            block_end = text.find('\n)', block_start)
            if block_end == -1:
                block_end = min(block_start + 500, len(text))
            block = text[block_start:block_end]
            desc_match = re.search(r'description\s*=\s*["\']([^"\']{5,})["\']', block)
            if desc_match:
                desc = desc_match.group(1)

            # If no description in register(), look for schema description
            if not desc:
                schema_pattern = rf'["\']name["\']\s*:\s*["\']({re.escape(tool_name)})["\'].*?["\']description["\']\s*:\s*["\']([^"\']+)["\']'
                schema_match = re.search(schema_pattern, text, re.DOTALL)
                if schema_match:
                    desc = schema_match.group(2)[:200]

            if not desc and docstring:
                desc = docstring.split('\n')[0][:200]

            tools.append({
                'name': tool_name,
                'toolset': toolset,
                'description': desc,
                'source_file': py_file.name,
            })

    return tools


# ── Hermes skill scanner ─────────────────────────────────────────────

def _scan_hermes_skills(repo: Path) -> list[dict]:
    """Extract skills from SKILL.md files with YAML frontmatter."""
    skills = []
    for skills_dir_name in ('skills', 'optional-skills'):
        skills_dir = repo / skills_dir_name
        if not skills_dir.is_dir():
            continue
        for md_file in sorted(skills_dir.rglob('SKILL.md')):
            try:
                text = md_file.read_text(errors='replace')
            except Exception:
                continue

            # Parse YAML frontmatter between --- markers
            fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if not fm_match:
                continue

            fm = fm_match.group(1)
            name = _yaml_value(fm, 'name') or md_file.parent.name
            desc = _yaml_value(fm, 'description') or ''
            tags_match = re.search(r'tags:\s*\[([^\]]+)\]', fm)
            tags = [t.strip().strip("'\"") for t in tags_match.group(1).split(',')] if tags_match else []

            skills.append({
                'name': name,
                'description': desc,
                'tags': tags,
                'source': f'{skills_dir_name}/{md_file.parent.name}',
                'optional': skills_dir_name == 'optional-skills',
            })

    return skills


def _yaml_value(text: str, key: str) -> str:
    """Extract a simple scalar value from YAML-like text."""
    m = re.search(rf'^{key}\s*:\s*(.+)$', text, re.MULTILINE)
    if m:
        val = m.group(1).strip().strip("'\"")
        return val
    return ''


# ── Hermes command scanner ───────────────────────────────────────────

def _scan_hermes_commands(repo: Path) -> list[dict]:
    """Extract CommandDef entries from hermes_cli/commands.py."""
    cmd_file = repo / 'hermes_cli' / 'commands.py'
    if not cmd_file.exists():
        return []

    try:
        text = cmd_file.read_text(errors='replace')
    except Exception:
        return []

    commands = []
    # Match CommandDef("name", "description", "category", ...)
    for m in re.finditer(
        r'CommandDef\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']',
        text
    ):
        name = m.group(1)
        desc = m.group(2)
        category = m.group(3)

        # Check for aliases
        aliases = []
        block_end = text.find(')', m.end())
        if block_end != -1:
            block = text[m.end():block_end]
            alias_match = re.search(r'aliases\s*=\s*\(([^)]+)\)', block)
            if alias_match:
                aliases = [a.strip().strip("'\"") for a in alias_match.group(1).split(',') if a.strip().strip("'\"")]

        commands.append({
            'name': f'/{name}',
            'description': desc,
            'category': category,
            'aliases': aliases,
        })

    return commands


# ── Architecture scanner ─────────────────────────────────────────────

def _scan_architecture(repo: Path, doc_files: list[str]) -> str:
    """Read key documentation files for architectural analysis."""
    sections = []
    for fname in doc_files:
        fpath = repo / fname
        if not fpath.exists():
            continue
        try:
            text = fpath.read_text(errors='replace')
            # Truncate large files to ~3000 chars for LLM context
            if len(text) > 3000:
                text = text[:3000] + '\n...(truncated)'
            sections.append(f'### {fname}\n{text}')
        except Exception:
            continue
    return '\n\n'.join(sections)


# ── Generic scanner for non-hermes repos ─────────────────────────────

def _scan_generic_tools(repo: Path, config: dict) -> list[dict]:
    """Best-effort tool extraction for TypeScript/Go repos."""
    tools_dir = repo / config.get('tools_dir', 'tools')
    if not tools_dir.is_dir():
        return []

    tools = []
    extensions = {'typescript': '*.ts', 'go': '*.go', 'python': '*.py'}
    ext = extensions.get(config.get('lang', ''), '*.*')

    for f in sorted(tools_dir.rglob(ext)):
        if f.name.startswith('_') or f.name.startswith('test'):
            continue
        name = f.stem.replace('_tool', '').replace('_', '-')
        # Try to read first line comment/docstring
        try:
            first_lines = f.read_text(errors='replace')[:500]
            desc_match = re.search(r'(?:description|desc)\s*[:=]\s*["`\']([^"`\']+)', first_lines)
            desc = desc_match.group(1) if desc_match else ''
        except Exception:
            desc = ''
        tools.append({
            'name': name,
            'toolset': 'unknown',
            'description': desc,
            'source_file': f.name,
        })

    return tools


# ── Charon capability collector ──────────────────────────────────────

def get_charon_capabilities(charon_root: Path) -> dict:
    """Collect Charon's current tools, commands, and features."""
    tools = []
    commands = []

    # Extract tool names from ALL_TOOL_DEFS
    tools_init = charon_root / 'apps' / 'core-daemon' / 'tools' / '__init__.py'
    if tools_init.exists():
        try:
            text = tools_init.read_text(errors='replace')
            for m in re.finditer(r"'(\w+)':\s*execute_", text):
                tools.append(m.group(1))
        except Exception:
            pass

    # Extract commands from chat_backend._command_catalog
    backend = charon_root / 'apps' / 'tui' / 'opentui' / 'chat_backend.py'
    if backend.exists():
        try:
            text = backend.read_text(errors='replace')
            for m in re.finditer(r"'cmd'\s*:\s*'(/[^']+)'", text):
                cmd = m.group(1).split(' ')[0]  # Just the base command
                if cmd not in commands:
                    commands.append(cmd)
        except Exception:
            pass

    # Read feature index if available
    features = []
    feature_idx = charon_root / 'docs' / 'features' / 'INDEX.md'
    if feature_idx.exists():
        try:
            text = feature_idx.read_text(errors='replace')
            for m in re.finditer(r'(F\d+)\s*[-—]\s*(.+)', text):
                features.append(f'{m.group(1)}: {m.group(2).strip()}')
        except Exception:
            pass

    return {
        'tools': tools,
        'commands': commands,
        'features': features,
    }


# ── LLM analysis (uses Charon's configured provider) ─────────────────

async def _provider_query(prompt: str, system: str, state_dir: Path, max_tokens: int = 4096) -> tuple[bool, str]:
    """Call the configured LLM provider (Codex, Claude, etc.) for analysis."""
    try:
        from provider_bridge import create_provider_and_model
        provider, model, ready = create_provider_and_model(state_dir)
        if not ready:
            return False, 'no provider configured'

        text_parts = []
        async for delta in provider.stream(
            messages=[{'role': 'user', 'content': prompt}],
            model=model,
            system_prompt=system,
            max_tokens=max_tokens,
        ):
            if hasattr(delta, 'type'):
                if delta.type == 'text':
                    text_parts.append(delta.text)
                elif delta.type == 'error':
                    return False, delta.error or 'LLM error'

        response = ''.join(text_parts).strip()
        return (True, response) if response else (False, 'empty response')
    except Exception as e:
        return False, f'provider query failed: {e}'


def _extract_json(text: str) -> list | dict | None:
    """Extract JSON from LLM response, handling markdown code blocks."""
    text = text.strip()
    if '```' in text:
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array or object in the text
        for pattern in [r'(\[.*\])', r'(\{.*\})']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
    return None


ANALYSIS_SYSTEM = (
    'You are a senior engineering architect analyzing agent capabilities for the Charon agent OS. '
    'Your job is to compare discovered abilities against what Charon already has, identify real gaps, '
    'and rank them by how much they would improve Charon. Be rigorous — if Charon already has an '
    'equivalent (even under a different name), mark it as charon_has=true. '
    'Respond ONLY with valid JSON, no other text.'
)


def llm_analyze_abilities(
    agent_name: str,
    tools: list[dict],
    skills: list[dict],
    commands: list[dict],
    charon_caps: dict,
    state_dir: Path,
    on_status: Callable[[str], None] | None = None,
) -> list[dict]:
    """Use the configured LLM provider to compare agent abilities against Charon."""

    emit = on_status or (lambda msg: None)

    # Format abilities compactly
    ability_lines = []
    for t in tools:
        ability_lines.append(f'- tool:{t["name"]} ({t["toolset"]}) — {t["description"][:120]}')
    for s in skills:
        ability_lines.append(f'- skill:{s["name"]} — {s["description"][:120]}')
    for c in commands:
        ability_lines.append(f'- command:{c["name"]} — {c["description"][:120]}')

    charon_tools = ', '.join(charon_caps['tools'])
    charon_commands = ', '.join(charon_caps['commands'][:40])
    charon_features = '\n'.join(f'  - {f}' for f in charon_caps.get('features', [])[:20])

    charon_summary = (
        f'Charon tools: {charon_tools}\n'
        f'Charon commands: {charon_commands}\n'
    )
    if charon_features:
        charon_summary += f'Charon features:\n{charon_features}\n'

    # Process in batches — larger batches since we're using a real model now
    batch_size = 40
    all_results = []

    for i in range(0, len(ability_lines), batch_size):
        batch = ability_lines[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(ability_lines) + batch_size - 1) // batch_size

        emit(f'Analyzing {agent_name} abilities (batch {batch_num}/{total_batches})...')

        prompt = f"""Analyze these {agent_name} abilities and compare against Charon's capabilities.

## {agent_name} abilities to evaluate:
{chr(10).join(batch)}

## Charon's current capabilities:
{charon_summary}

## Instructions:
For EACH ability listed above, produce a JSON object with these fields:
- "name": the ability name (exactly as listed)
- "type": "tool", "skill", or "command"
- "description": 1-sentence description of what it does
- "charon_has": true if Charon already has an equivalent capability (even under a different name like Read vs read_file, Web vs web_search, Bash vs terminal, etc.), false if genuinely new
- "priority": one of "critical", "high", "medium", "low", "skip"
- "rationale": 1 sentence explaining WHY this priority — what would Charon gain or why it's not needed

Priority guide:
- "skip": Charon already has this (charon_has=true), OR it's platform-specific/irrelevant to Charon's use case
- "critical": Major capability gap — without this, Charon can't do entire categories of work
- "high": Significant gap — adds a genuinely new and useful capability
- "medium": Useful enhancement but not blocking
- "low": Nice to have, niche, or Charon has a partial equivalent

Be aggressive about marking "skip" — many tools/skills will have Charon equivalents under different names.

Return a JSON array of objects. ONLY the JSON array, nothing else."""

        try:
            ok, resp = asyncio.run(_provider_query(prompt, ANALYSIS_SYSTEM, state_dir))
        except Exception as e:
            ok, resp = False, str(e)

        if ok:
            parsed = _extract_json(resp)
            if isinstance(parsed, list):
                all_results.extend(parsed)
                skip_count = sum(1 for a in parsed if a.get('priority') == 'skip' or a.get('charon_has'))
                new_count = len(parsed) - skip_count
                emit(f'  Batch {batch_num}: {new_count} new, {skip_count} skipped')
            else:
                emit(f'  Batch {batch_num}: failed to parse LLM response, using fallback')
                all_results.extend(_heuristic_analyze(batch, charon_caps))
        else:
            emit(f'  LLM unavailable ({resp[:80]}), using heuristic...')
            all_results.extend(_heuristic_analyze(batch, charon_caps))

    return all_results


def _heuristic_analyze(ability_lines: list[str], charon_caps: dict) -> list[dict]:
    """Fallback: simple string matching when no LLM is available."""
    results = []
    for line in ability_lines:
        parts = line.split(' — ', 1)
        name_part = parts[0].replace('- tool:', '').replace('- skill:', '').replace('- command:', '').strip()
        name_part = name_part.split(' (')[0]
        atype = 'tool' if 'tool:' in line else 'skill' if 'skill:' in line else 'command'

        charon_has = any(
            name_part.lower().replace('-', '').replace('_', '') in t.lower().replace('-', '').replace('_', '')
            or t.lower().replace('-', '').replace('_', '') in name_part.lower().replace('-', '').replace('_', '')
            for t in charon_caps['tools']
        )
        results.append({
            'name': name_part,
            'type': atype,
            'description': parts[1] if len(parts) > 1 else '',
            'charon_has': charon_has,
            'priority': 'skip' if charon_has else 'medium',
            'rationale': 'Equivalent exists in Charon' if charon_has else '(No LLM — needs manual review)',
        })
    return results


# ── Document generation ──────────────────────────────────────────────

PRIORITY_ORDER = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'skip': 4}


def generate_registry_doc(
    scan_results: dict[str, dict],
    unavailable: list[str],
    output_path: Path,
) -> str:
    """Generate docs/agent-abilities-registry.md."""

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    scanned = list(scan_results.keys())

    lines = [
        '# Agent Abilities Registry',
        '',
        f'> Auto-generated by `/harvest_souls`. Last scan: {timestamp}',
        f'> Repos scanned: {", ".join(scanned) or "none"} | Unavailable: {", ".join(unavailable) or "none"}',
        '',
        '---',
        '',
    ]

    # Assimilated abilities (collected across all agents)
    assimilated: list[dict] = []

    for agent_name, data in scan_results.items():
        tools = data.get('tools', [])
        skills = data.get('skills', [])
        commands = data.get('commands', [])
        analysis = data.get('analysis', [])
        arch_notes = data.get('architecture', '')

        # Count new abilities
        new_count = sum(1 for a in analysis if not a.get('charon_has', True))

        lines.append(f'## {agent_name}')
        lines.append('')

        # Tools table
        lines.append(f'### Tools ({len(tools)} discovered, {sum(1 for a in analysis if a.get("type") == "tool" and not a.get("charon_has"))} new to Charon)')
        lines.append('')
        lines.append('| Tool | Toolset | Description | Charon Has | Priority |')
        lines.append('|------|---------|-------------|------------|----------|')
        for t in tools:
            # Find matching analysis entry
            match = next((a for a in analysis if a['name'] == t['name'] and a.get('type') == 'tool'), None)
            has = 'yes' if match and match.get('charon_has') else 'no' if match else '?'
            pri = match.get('priority', '?') if match else '?'
            lines.append(f'| {t["name"]} | {t["toolset"]} | {t["description"][:80]} | {has} | {pri} |')
        lines.append('')

        # Skills table
        if skills:
            lines.append(f'### Skills ({len(skills)} discovered)')
            lines.append('')
            lines.append('| Skill | Tags | Description | Priority |')
            lines.append('|-------|------|-------------|----------|')
            for s in skills:
                match = next((a for a in analysis if a['name'] == s['name'] and a.get('type') == 'skill'), None)
                pri = match.get('priority', '?') if match else '?'
                tags = ', '.join(s.get('tags', [])[:4])
                lines.append(f'| {s["name"]} | {tags} | {s["description"][:80]} | {pri} |')
            lines.append('')

        # Commands table
        if commands:
            lines.append(f'### Commands ({len(commands)} discovered)')
            lines.append('')
            lines.append('| Command | Category | Description | Charon Equivalent |')
            lines.append('|---------|----------|-------------|-------------------|')
            for c in commands:
                match = next((a for a in analysis if a['name'] == c['name'] and a.get('type') == 'command'), None)
                has = 'yes' if match and match.get('charon_has') else '-'
                lines.append(f'| {c["name"]} | {c["category"]} | {c["description"][:80]} | {has} |')
            lines.append('')

        # Architecture section
        if arch_notes:
            lines.append('### Architectural Notes')
            lines.append('')
            # Extract innovation-worthy items from analysis
            innovations = [a for a in analysis if a.get('priority') in ('critical', 'high') and not a.get('charon_has')]
            if innovations:
                for inn in innovations:
                    lines.append(f'- **{inn["name"]}**: {inn.get("rationale", inn.get("description", ""))}')
            else:
                lines.append('(No critical architectural gaps identified)')
            lines.append('')

        lines.append('---')
        lines.append('')

        # Collect assimilated abilities (non-skip, charon doesn't have)
        for a in analysis:
            if not a.get('charon_has') and a.get('priority', 'skip') != 'skip':
                assimilated.append({**a, 'source_agent': agent_name})

    # Unavailable repos
    for name in unavailable:
        lines.append(f'## {name}')
        lines.append('')
        lines.append(f'(unavailable — not cloned locally at `~/Projects/{name}`)')
        lines.append('')
        lines.append('---')
        lines.append('')

    # Assimilated Abilities section
    lines.append('## Assimilated Abilities')
    lines.append('')
    lines.append('Capabilities Charon should implement, ranked by priority from gap analysis.')
    lines.append('')

    # Sort by priority
    assimilated.sort(key=lambda a: PRIORITY_ORDER.get(a.get('priority', 'low'), 3))

    for tier in ('critical', 'high', 'medium', 'low'):
        tier_items = [a for a in assimilated if a.get('priority') == tier]
        if not tier_items:
            continue
        lines.append(f'### {tier.capitalize()}')
        lines.append('')
        lines.append('| # | Ability | Type | Source | Description | Rationale |')
        lines.append('|---|---------|------|--------|-------------|-----------|')
        for i, a in enumerate(tier_items, 1):
            lines.append(f'| {i} | {a["name"]} | {a.get("type", "?")} | {a["source_agent"]} | {a.get("description", "")[:60]} | {a.get("rationale", "")[:80]} |')
        lines.append('')

    if not assimilated:
        lines.append('No new abilities identified.')
        lines.append('')

    doc_text = '\n'.join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(doc_text)
    return str(output_path)


# ── State persistence ────────────────────────────────────────────────

def save_scan_state(state_dir: Path, results: dict[str, dict], unavailable: list[str]) -> None:
    """Persist scan results to .charon_state/assimilation/."""
    assim_dir = state_dir / 'assimilation'
    assim_dir.mkdir(parents=True, exist_ok=True)
    abilities_dir = assim_dir / 'abilities'
    abilities_dir.mkdir(exist_ok=True)

    for agent_name, data in results.items():
        (abilities_dir / f'{agent_name}.json').write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )

    summary = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'repos_scanned': list(results.keys()),
        'repos_unavailable': unavailable,
        'total_tools': sum(len(d.get('tools', [])) for d in results.values()),
        'total_skills': sum(len(d.get('skills', [])) for d in results.values()),
        'total_commands': sum(len(d.get('commands', [])) for d in results.values()),
        'total_abilities_analyzed': sum(len(d.get('analysis', [])) for d in results.values()),
        'new_abilities': sum(
            1 for d in results.values()
            for a in d.get('analysis', [])
            if not a.get('charon_has') and a.get('priority', 'skip') != 'skip'
        ),
    }
    (assim_dir / 'last_scan.json').write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )


def load_last_scan(state_dir: Path) -> dict | None:
    """Load the most recent scan summary."""
    scan_file = state_dir / 'assimilation' / 'last_scan.json'
    if not scan_file.exists():
        return None
    try:
        return json.loads(scan_file.read_text())
    except Exception:
        return None


# ── Orchestrator ─────────────────────────────────────────────────────

def run_full_assimilation(
    state_dir: Path,
    docs_dir: Path,
    charon_root: Path,
    agent_filter: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict:
    """Run the full assimilation pipeline.

    1. Discover available repos
    2. Scan each for tools, skills, commands, architecture
    3. Collect Charon capabilities
    4. LLM-analyze abilities and compare
    5. Generate registry document
    6. Save state
    """
    emit = on_status or (lambda msg: None)

    # 1. Discover repos (clone missing ones into .charon_state/assimilation/repos/)
    available, unavailable = scan_available_repos(
        state_dir=state_dir, agent_filter=agent_filter, auto_clone=True, on_status=on_status,
    )

    if not available:
        emit(f'No agent repos available. Checked: {", ".join(list(AGENT_REPOS.keys()))}')
        return {'repos_scanned': 0, 'total_abilities': 0, 'new_abilities': 0}

    emit(f'Found {len(available)} repo(s): {", ".join(available.keys())}')
    if unavailable:
        emit(f'Unavailable: {", ".join(unavailable)}')

    # 2. Collect Charon capabilities
    emit('Collecting Charon capabilities...')
    charon_caps = get_charon_capabilities(charon_root)
    emit(f'Charon has {len(charon_caps["tools"])} tools, {len(charon_caps["commands"])} commands')

    # 3. Scan each repo
    results: dict[str, dict] = {}

    for agent_name, repo_path in available.items():
        config = AGENT_REPOS[agent_name]
        emit(f'Scanning {agent_name}...')

        # Tools
        if agent_name == 'hermes-agent':
            tools = _scan_hermes_tools(repo_path)
        else:
            tools = _scan_generic_tools(repo_path, config)
        emit(f'  {len(tools)} tools found')

        # Skills
        if agent_name == 'hermes-agent':
            skills = _scan_hermes_skills(repo_path)
        else:
            skills = []
        emit(f'  {len(skills)} skills found')

        # Commands
        if agent_name == 'hermes-agent':
            commands = _scan_hermes_commands(repo_path)
        else:
            commands = []
        emit(f'  {len(commands)} commands found')

        # Architecture
        arch = _scan_architecture(repo_path, config.get('docs', []))

        # 4. LLM analysis (uses configured provider — Codex, Claude, etc.)
        analysis = llm_analyze_abilities(
            agent_name, tools, skills, commands, charon_caps,
            state_dir=state_dir,
            on_status=on_status,
        )

        results[agent_name] = {
            'tools': tools,
            'skills': skills,
            'commands': commands,
            'architecture': arch,
            'analysis': analysis,
        }

        new_count = sum(1 for a in analysis if not a.get('charon_has') and a.get('priority', 'skip') != 'skip')
        emit(f'  {len(analysis)} abilities analyzed, {new_count} new to Charon')

    # 5. Generate document
    emit('Generating registry document...')
    output_path = docs_dir / 'agent-abilities-registry.md'
    generate_registry_doc(results, unavailable, output_path)
    emit(f'Registry written to {output_path.relative_to(charon_root)}')

    # 6. Save state
    save_scan_state(state_dir, results, unavailable)

    total = sum(len(d.get('analysis', [])) for d in results.values())
    new_total = sum(
        1 for d in results.values()
        for a in d.get('analysis', [])
        if not a.get('charon_has') and a.get('priority', 'skip') != 'skip'
    )

    return {
        'repos_scanned': len(results),
        'total_abilities': total,
        'new_abilities': new_total,
    }
