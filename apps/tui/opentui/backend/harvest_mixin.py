"""/harvest_souls workflow mixin."""
from __future__ import annotations

import json

from backend import common


class HarvestMixin:
    """The /harvest_souls review/adopt workflow."""

    def _harvest_souls_load_findings(self) -> list[dict]:
        """Load and sort the new abilities from the last scan."""
        from charon.memory.assimilation import PRIORITY_ORDER
        abilities_dir = common.STATE_DIR / 'assimilation' / 'abilities'
        if not abilities_dir.is_dir():
            return []
        all_new = []
        for f in abilities_dir.glob('*.json'):
            try:
                data = json.loads(f.read_text())
                agent = f.stem
                for a in data.get('analysis', []):
                    if not a.get('charon_has') and a.get('priority', 'skip') != 'skip':
                        all_new.append({**a, 'source': agent})
            except Exception:
                pass
        all_new.sort(key=lambda a: PRIORITY_ORDER.get(a.get('priority', 'low'), 3))
        return all_new

    def _harvest_souls_show_findings(self, request_id: str | None):
        """Display numbered findings with next-step instructions."""
        findings = self._harvest_souls_load_findings()
        if not findings:
            common.emit({'type': 'status', 'message': 'No new abilities found. Run /harvest_souls to scan.', 'request_id': request_id})
            return

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '═══ Souls harvested ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})

        current_tier = None
        for i, a in enumerate(findings):
            tier = a.get('priority', 'medium').upper()
            if tier != current_tier:
                current_tier = tier
                count = sum(1 for x in findings if x.get('priority', 'medium').upper() == tier)
                common.emit({'type': 'status', 'message': f'  [{tier}] — {count} abilities', 'request_id': request_id})

            name = a.get('name', '?')
            desc = a.get('description', '')[:70]
            atype = a.get('type', '?')
            rationale = a.get('rationale', '')

            line = f'    {i + 1:>3}. {name}'
            if atype != '?':
                line += f' ({atype})'
            common.emit({'type': 'status', 'message': line, 'request_id': request_id})
            detail = rationale if rationale not in ('', '(needs manual review)', 'Heuristic match') else desc
            if detail:
                common.emit({'type': 'status', 'message': f'         {detail}', 'request_id': request_id})

        # Check what's already adopted
        adopted_file = common.STATE_DIR / 'assimilation' / 'adopted.json'
        adopted_count = 0
        if adopted_file.exists():
            try:
                adopted_count = len(json.loads(adopted_file.read_text()))
            except Exception:
                pass

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'{len(findings)} abilities available | {adopted_count} already adopted', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Next steps:', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls plan <N>         — see implementation path for ability #N', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls adopt <N>        — mark ability #N for adoption', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls adopt 1,3,7      — adopt multiple abilities', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls adopt all        — adopt everything', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls roadmap          — show adoption roadmap', 'request_id': request_id})

    def _harvest_souls_load_gap_review(self) -> list[dict]:
        """Load capability-level gap review clusters."""
        try:
            from charon.memory.assimilation import load_gap_review, PRIORITY_ORDER
            clusters = load_gap_review(common.STATE_DIR)
            clusters.sort(key=lambda c: (PRIORITY_ORDER.get(c.get('priority', 'low'), 3), -int(c.get('value', 0) or 0)))
            return clusters
        except Exception:
            return []

    def _harvest_souls_review(self, request_id: str | None):
        """Display capability-level harvest decisions for the user."""
        clusters = self._harvest_souls_load_gap_review()
        if not clusters:
            common.emit({'type': 'status', 'message': 'No capability gap review found. Run /harvest_souls first, or /harvest_souls evaluate after a scan.', 'request_id': request_id})
            return

        actionable = [c for c in clusters if c.get('recommendation') in ('assimilate', 'adapt') and c.get('priority') != 'skip']
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '═══ Harvest Souls: Capability Gap Review ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})

        current_tier = None
        for i, c in enumerate(clusters):
            tier = c.get('priority', 'medium').upper()
            if tier != current_tier:
                current_tier = tier
                count = sum(1 for x in clusters if x.get('priority', 'medium').upper() == tier)
                common.emit({'type': 'status', 'message': f'  [{tier}] — {count} capability clusters', 'request_id': request_id})

            cap = c.get('capability', '?')
            rec = c.get('recommendation', '?')
            coverage = c.get('charon_coverage', '?')
            real_gap = 'gap' if c.get('real_gap') else 'no gap'
            scores = f'V{c.get("value", "?")}/E{c.get("effort", "?")}/R{c.get("risk", "?")}'
            common.emit({'type': 'status', 'message': f'    {i + 1:>3}. {cap} — {rec} ({coverage}, {real_gap}, {scores})', 'request_id': request_id})
            rationale = c.get('rationale', '')
            if rationale:
                common.emit({'type': 'status', 'message': f'         {rationale[:160]}', 'request_id': request_id})
            src = c.get('source_items', [])[:5]
            if src:
                names = ', '.join(f'{x.get("source_agent", "?")}:{x.get("name", "?")}' for x in src)
                suffix = '…' if len(c.get('source_items', [])) > 5 else ''
                common.emit({'type': 'status', 'message': f'         Sources: {names}{suffix}', 'request_id': request_id})

        adopted_file = common.STATE_DIR / 'assimilation' / 'adopted_capabilities.json'
        adopted_count = 0
        if adopted_file.exists():
            try:
                adopted_count = len(json.loads(adopted_file.read_text()))
            except Exception:
                pass

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'{len(actionable)} actionable candidates | {adopted_count} capability clusters adopted', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Review doc: docs/agent-capability-gap-review.md', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Next steps:', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls decide <N>       — inspect one capability decision', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls harvest <N>      — add one capability cluster to adoption queue', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls harvest 1,3,7    — harvest multiple clusters', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls harvest all      — harvest all assimilate/adapt recommendations', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls roadmap          — show adoption roadmap', 'request_id': request_id})

    def _harvest_souls_decide(self, idx_str: str, request_id: str | None):
        """Show a detailed capability-cluster harvest decision."""
        clusters = self._harvest_souls_load_gap_review()
        if not clusters:
            common.emit({'type': 'status', 'message': 'No capability review. Run /harvest_souls first.', 'request_id': request_id})
            return
        try:
            idx = int(idx_str) - 1
            if idx < 0 or idx >= len(clusters):
                raise ValueError()
        except ValueError:
            common.emit({'type': 'error', 'error': f'Invalid capability number. Use 1-{len(clusters)}.', 'request_id': request_id})
            return
        c = clusters[idx]
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'═══ Harvest Decision: {c.get("capability", "?")} ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Recommendation: {c.get("recommendation", "?")} / {c.get("priority", "medium").upper()}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Coverage:       {c.get("charon_coverage", "?")}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Real gap:       {bool(c.get("real_gap"))}', 'request_id': request_id})
        if c.get('existing_charon_equivalent'):
            common.emit({'type': 'status', 'message': f'  Equivalent:     {c.get("existing_charon_equivalent")}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Scores:         value {c.get("value", "?")}/10, effort {c.get("effort", "?")}/10, risk {c.get("risk", "?")}/10', 'request_id': request_id})
        if c.get('rationale'):
            common.emit({'type': 'status', 'message': f'  Why:            {c.get("rationale")}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  Source items:', 'request_id': request_id})
        for item in c.get('source_items', [])[:20]:
            common.emit({'type': 'status', 'message': f'    - {item.get("source_agent", "?")}:{item.get("name", "?")} ({item.get("type", "?")})', 'request_id': request_id})
        plan = c.get('assimilation_plan') or []
        if plan:
            common.emit({'type': 'status', 'message': '', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '  Assimilation plan:', 'request_id': request_id})
            for n, step in enumerate(plan, 1):
                common.emit({'type': 'status', 'message': f'    {n}. {step}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  /harvest_souls harvest {idx + 1}  — add this capability to the adoption queue', 'request_id': request_id})

    def _harvest_souls_harvest(self, selection: str, request_id: str | None):
        """Adopt capability clusters selected by the user."""
        clusters = self._harvest_souls_load_gap_review()
        if not clusters:
            common.emit({'type': 'status', 'message': 'No capability review. Run /harvest_souls first.', 'request_id': request_id})
            return
        indices = set()
        if selection.lower() == 'all':
            indices = {i for i, c in enumerate(clusters) if c.get('recommendation') in ('assimilate', 'adapt') and c.get('priority') != 'skip'}
        else:
            for part in selection.split(','):
                part = part.strip()
                if '-' in part:
                    try:
                        a, b = part.split('-', 1)
                        for i in range(int(a) - 1, int(b)):
                            if 0 <= i < len(clusters):
                                indices.add(i)
                    except ValueError:
                        pass
                elif part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(clusters):
                        indices.add(idx)
        if not indices:
            common.emit({'type': 'error', 'error': f'Invalid selection. Use 1-{len(clusters)}, comma-separated, range, or all.', 'request_id': request_id})
            return
        adopted_file = common.STATE_DIR / 'assimilation' / 'adopted_capabilities.json'
        adopted = []
        if adopted_file.exists():
            try:
                adopted = json.loads(adopted_file.read_text())
            except Exception:
                pass
        existing = {c.get('id') or c.get('capability') for c in adopted}
        new_adoptions = []
        for i in sorted(indices):
            c = clusters[i]
            key = c.get('id') or c.get('capability')
            if key not in existing:
                c = {**c, 'impl_status': c.get('impl_status', 'pending')}
                new_adoptions.append(c)
                adopted.append(c)
        adopted_file.parent.mkdir(parents=True, exist_ok=True)
        adopted_file.write_text(json.dumps(adopted, indent=2, ensure_ascii=False))
        if new_adoptions:
            common.emit({'type': 'status', 'message': '', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'Queued {len(new_adoptions)} capability cluster(s) for assimilation:', 'request_id': request_id})
            for c in new_adoptions:
                common.emit({'type': 'status', 'message': f'  + {c.get("capability", "?")} ({c.get("recommendation", "?")}, {c.get("priority", "medium")})', 'request_id': request_id})
        else:
            common.emit({'type': 'status', 'message': 'All selected capability clusters were already queued.', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Total queued clusters: {len(adopted)} | /harvest_souls roadmap to see the plan', 'request_id': request_id})

    def _harvest_souls_plan(self, idx_str: str, request_id: str | None):
        """Show the implementation path for a specific ability."""
        findings = self._harvest_souls_load_findings()
        if not findings:
            common.emit({'type': 'status', 'message': 'No findings. Run /harvest_souls first.', 'request_id': request_id})
            return

        try:
            idx = int(idx_str) - 1
            if idx < 0 or idx >= len(findings):
                raise ValueError()
        except ValueError:
            common.emit({'type': 'error', 'error': f'Invalid ability number. Use 1-{len(findings)}.', 'request_id': request_id})
            return

        a = findings[idx]
        name = a.get('name', '?')
        atype = a.get('type', '?')
        source = a.get('source', '?')
        desc = a.get('description', '')
        priority = a.get('priority', 'medium').upper()
        rationale = a.get('rationale', '')

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'═══ Implementation Plan: {name} ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Source:    {source}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Type:      {atype}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  Priority:  {priority}', 'request_id': request_id})
        if desc:
            common.emit({'type': 'status', 'message': f'  What:      {desc}', 'request_id': request_id})
        if rationale and rationale not in ('(needs manual review)', 'Heuristic match'):
            common.emit({'type': 'status', 'message': f'  Why:       {rationale}', 'request_id': request_id})

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})

        # Generate implementation steps based on type
        if atype == 'tool':
            common.emit({'type': 'status', 'message': '  Onboarding path:', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'    1. Study reference: ~/{source}/{a.get("source_file", "tools/")}'
                  if a.get('source_file') else f'    1. Study reference in {source} repo', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'    2. Create src/charon/tools/{name.replace("-", "_")}_tool.py', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'    3. Define {name.upper()}_TOOL_DEF schema + execute_{name.replace("-", "_")}() handler', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    4. Register in src/charon/tools/__init__.py (ALL_TOOL_DEFS + TOOL_EXECUTORS)', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    5. Test: /tools should list the new tool', 'request_id': request_id})
        elif atype == 'skill':
            common.emit({'type': 'status', 'message': '  Onboarding path:', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'    1. Study reference: ~/{source}/skills/{name}/SKILL.md', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    2. Adapt skill content for Charon\'s context', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    3. Add to Charon\'s skills registry or system prompt', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    4. Test: verify skill is available and functional', 'request_id': request_id})
        elif atype == 'command':
            common.emit({'type': 'status', 'message': '  Onboarding path:', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'    1. Study reference: ~/{source}/hermes_cli/commands.py', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    2. Add handler in apps/tui/opentui/chat_backend.py handle_command()', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    3. Register in _command_catalog() and chat.rs command_suggestions()', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    4. Rebuild TUI: cargo build --release', 'request_id': request_id})
        else:
            common.emit({'type': 'status', 'message': '  Onboarding path:', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'    1. Study the reference implementation in {source}', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    2. Design Charon-native equivalent', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '    3. Implement and test', 'request_id': request_id})

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  /harvest_souls adopt {idx + 1}  — adopt this ability', 'request_id': request_id})

    def _harvest_souls_adopt(self, selection: str, request_id: str | None):
        """Mark abilities for adoption."""
        findings = self._harvest_souls_load_findings()
        if not findings:
            common.emit({'type': 'status', 'message': 'No findings. Run /harvest_souls first.', 'request_id': request_id})
            return

        # Parse selection: "all", "3", "1,3,7", "1-5"
        indices = set()
        if selection.lower() == 'all':
            indices = set(range(len(findings)))
        else:
            for part in selection.split(','):
                part = part.strip()
                if '-' in part:
                    try:
                        a, b = part.split('-', 1)
                        for i in range(int(a) - 1, int(b)):
                            if 0 <= i < len(findings):
                                indices.add(i)
                    except ValueError:
                        pass
                elif part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(findings):
                        indices.add(idx)

        if not indices:
            common.emit({'type': 'error', 'error': f'Invalid selection. Use a number (1-{len(findings)}), comma-separated, range (1-5), or "all".', 'request_id': request_id})
            return

        # Load existing adopted list
        adopted_file = common.STATE_DIR / 'assimilation' / 'adopted.json'
        adopted = []
        if adopted_file.exists():
            try:
                adopted = json.loads(adopted_file.read_text())
            except Exception:
                pass
        existing_names = {a['name'] for a in adopted}

        new_adoptions = []
        for i in sorted(indices):
            a = findings[i]
            if a['name'] not in existing_names:
                new_adoptions.append(a)
                adopted.append(a)

        adopted_file.parent.mkdir(parents=True, exist_ok=True)
        adopted_file.write_text(json.dumps(adopted, indent=2, ensure_ascii=False))

        if new_adoptions:
            common.emit({'type': 'status', 'message': '', 'request_id': request_id})
            common.emit({'type': 'status', 'message': f'Adopted {len(new_adoptions)} new abilities:', 'request_id': request_id})
            for a in new_adoptions:
                common.emit({'type': 'status', 'message': f'  + {a["name"]} ({a.get("type", "?")}, from {a.get("source", "?")})', 'request_id': request_id})
            skipped = len(indices) - len(new_adoptions)
            if skipped:
                common.emit({'type': 'status', 'message': f'  ({skipped} already adopted)', 'request_id': request_id})
        else:
            common.emit({'type': 'status', 'message': 'All selected abilities were already adopted.', 'request_id': request_id})

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Total adopted: {len(adopted)} | /harvest_souls roadmap to see the plan', 'request_id': request_id})

    def _harvest_souls_roadmap(self, request_id: str | None):
        """Show the adoption roadmap — what's been adopted and implementation status."""
        adopted_file = common.STATE_DIR / 'assimilation' / 'adopted_capabilities.json'
        legacy_adopted_file = common.STATE_DIR / 'assimilation' / 'adopted.json'
        if not adopted_file.exists() and legacy_adopted_file.exists():
            adopted_file = legacy_adopted_file
        if not adopted_file.exists():
            common.emit({'type': 'status', 'message': 'No abilities adopted yet. Run /harvest_souls, then /harvest_souls harvest <N>.', 'request_id': request_id})
            return

        try:
            adopted = json.loads(adopted_file.read_text())
        except Exception:
            common.emit({'type': 'error', 'error': 'Failed to load adopted abilities.', 'request_id': request_id})
            return

        if not adopted:
            common.emit({'type': 'status', 'message': 'No abilities adopted yet.', 'request_id': request_id})
            return

        from charon.memory.assimilation import PRIORITY_ORDER
        adopted.sort(key=lambda a: PRIORITY_ORDER.get(a.get('priority', 'low'), 3))

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '═══ Adoption Roadmap ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})

        current_tier = None
        for _i, a in enumerate(adopted):
            tier = a.get('priority', 'medium').upper()
            if tier != current_tier:
                current_tier = tier
                common.emit({'type': 'status', 'message': f'  [{tier}]', 'request_id': request_id})

            name = a.get('capability') or a.get('name', '?')
            atype = a.get('type') or a.get('recommendation', '?')
            source = a.get('source') or ','.join(a.get('source_agents', [])[:2]) or '?'
            status = a.get('impl_status', 'pending')
            marker = '[ ]' if status == 'pending' else '[~]' if status == 'in_progress' else '[x]'
            common.emit({'type': 'status', 'message': f'    {marker} {name} ({atype}, from {source})', 'request_id': request_id})

        pending = sum(1 for a in adopted if a.get('impl_status', 'pending') == 'pending')
        in_prog = sum(1 for a in adopted if a.get('impl_status') == 'in_progress')
        done = sum(1 for a in adopted if a.get('impl_status') == 'done')

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Pending: {pending} | In progress: {in_prog} | Done: {done}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /harvest_souls plan <N>  — implementation steps for any ability', 'request_id': request_id})
