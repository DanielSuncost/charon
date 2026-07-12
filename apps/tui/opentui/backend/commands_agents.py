"""Agent/fleet slash-command handlers: specialist, hermes/pi, fleet, voyage,
add-remote, harvest_souls, shades.

Branch bodies are preserved verbatim from the original ``handle_command``
if/elif router in ``commands_mixin.py``; only the method wrappers and the
trailing ``return UNHANDLED`` are new. See ``CommandsMixin.handle_command``
for the dispatch.
"""
from __future__ import annotations

import json

from backend import common
from backend.commands_mixin import UNHANDLED

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


class AgentCommandsMixin:
    """Handlers for the specialist/agent/fleet/harvest command families."""

    def _cmd_hermes_pi(self, command: str, request_id: str | None):
        if command in ('/hermes', '/pi'):
            try:
                from charon.fleet.external_session_launcher import launch_wrapped_session
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                project = str(onboarding.get('project') or str(common.ROOT)).strip()
                agent_kind = command[1:]
                result = launch_wrapped_session(
                    state_dir=common.STATE_DIR,
                    project_root=project,
                    agent_kind=agent_kind,
                )
                if result.get('ok'):
                    display_name = str(result.get('display_name') or agent_kind)
                    session_name = str(result.get('tmux_session') or result.get('session_name') or '')
                    self._register_owned_boat_session(session_name)
                    common.emit({'type': 'status', 'message': f'✓ {display_name} session created: {session_name}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': 'To view or interact with it, press F3 for the sessions grid.', 'request_id': request_id})
                    self.handle_refresh(request_id)
                else:
                    common.emit({'type': 'error', 'error': f'Failed to create {agent_kind} session: {result.get("error", "unknown error")}', 'request_id': request_id})
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Failed to create external session: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_add_remote(self, command: str, request_id: str | None):
        if command == '/add-remote' or command.startswith('/add-remote '):
            rest = command[12:].strip() if command.startswith('/add-remote ') else ''
            try:
                from charon.fleet.remote_onboard import test_ssh, check_boat_installed, deploy_boat_remote, discover_remote_agents, auto_configure_fleet

                if not rest:
                    common.emit({'type': 'status', 'message': (
                        'Usage: /add-remote <host> [user]\n'
                        '  /add-remote 55.55.55.55 ubuntu    — test SSH and discover agents\n'
                        '  /add-remote confirm                — save discovered config\n'
                        '  /add-remote cancel                 — discard pending onboarding'
                    ), 'request_id': request_id})
                    return

                if rest == 'cancel':
                    self._pending_remote_onboard = None
                    common.emit({'type': 'status', 'message': 'Remote onboarding cancelled.', 'request_id': request_id})
                    return

                if rest == 'confirm':
                    pending = self._pending_remote_onboard
                    if not pending:
                        common.emit({'type': 'error', 'error': 'No pending remote onboard. Run /add-remote <host> first.', 'request_id': request_id})
                        return
                    all_agents = pending.get('agents', [])
                    if not all_agents:
                        common.emit({'type': 'error', 'error': 'No agents to add. Run /add-remote <host> to discover agents first.', 'request_id': request_id})
                        return
                    server = auto_configure_fleet(
                        host=pending['host'],
                        user=pending.get('user', ''),
                        agents=all_agents,
                    )
                    self._pending_remote_onboard = None
                    # Start fleet sync if not already running
                    try:
                        from charon.fleet.fleet_sync import start_fleet_sync
                        start_fleet_sync()
                    except Exception as exc:
                        _diag('commands_mixin', 'fleet sync auto-start failed after remote onboard', error=exc)
                    common.emit({'type': 'status', 'message': (
                        f'Added server "{server["id"]}" ({server["host"]}) with '
                        f'{len(server.get("agents", []))} agent(s) to fleet.\n'
                        f'Remote sessions will appear in F3 grid shortly.'
                    ), 'request_id': request_id})
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    return

                # Parse host and optional user
                parts = rest.split()
                host = parts[0]
                user = parts[1] if len(parts) > 1 else ''

                # Step 1: Test SSH
                common.emit({'type': 'status', 'message': f'Testing SSH to {user + "@" if user else ""}{host}...', 'request_id': request_id})
                ssh_ok, ssh_msg = test_ssh(host, user)
                common.emit({'type': 'status', 'message': ssh_msg, 'request_id': request_id})
                if not ssh_ok:
                    if not user:
                        common.emit({'type': 'status', 'message': 'Tip: try with a username: /add-remote <host> <user>', 'request_id': request_id})
                    return

                # Step 2: Check boat
                common.emit({'type': 'status', 'message': 'Checking for charons-boat on remote...', 'request_id': request_id})
                boat_ok, boat_msg = check_boat_installed(host, user)
                if boat_ok:
                    common.emit({'type': 'status', 'message': f'charons-boat found: {boat_msg}', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': 'charons-boat not found. Deploying...', 'request_id': request_id})
                    dep_ok, dep_msg = deploy_boat_remote(host, user)
                    if dep_ok:
                        common.emit({'type': 'status', 'message': f'Deployed: {dep_msg}', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': f'Deploy failed: {dep_msg}\nContinuing with tmux discovery...', 'request_id': request_id})

                # Step 3: Discover agents
                common.emit({'type': 'status', 'message': 'Discovering agents...', 'request_id': request_id})
                discovery = discover_remote_agents(host, user)
                boat_agents = discovery.get('boat_sessions', [])
                tmux_agents = discovery.get('tmux_agents', [])
                tmux_other = discovery.get('tmux_other', [])
                all_agents = boat_agents + tmux_agents

                if not all_agents and not tmux_other:
                    common.emit({'type': 'status', 'message': 'No agents found on remote. You can still add the server manually.', 'request_id': request_id})
                    # Save pending with empty agents so user can still confirm (creates empty server entry)
                    self._pending_remote_onboard = {'host': host, 'user': user, 'agents': []}
                    return

                lines = [f'Found {len(all_agents)} agent(s) on {host}:']
                for i, a in enumerate(all_agents, 1):
                    source = 'boat-wrapped' if a.get('source') == 'boat' else 'tmux session'
                    lines.append(f'  {i}. {a["name"]} ({a["type"]}) — {source}, {a["status"]}')
                if tmux_other:
                    lines.append(f'\nAlso found {len(tmux_other)} other tmux session(s):')
                    for a in tmux_other:
                        lines.append(f'  - {a["name"]} (unrecognized agent)')
                if tmux_agents:
                    lines.append('\nTmux agents will be auto-bridged via boat\'s tmux discovery.')
                lines.append('\nType /add-remote confirm to save, or /add-remote cancel to abort.')

                self._pending_remote_onboard = {
                    'host': host,
                    'user': user,
                    'agents': all_agents,
                    'discovery': discovery,
                }
                common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Remote onboarding failed: {e}', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_shades(self, command: str, request_id: str | None):
        if command == '/shades' or command == '/shade stats':
            try:
                from charon.shade.shade_stats import get_shade_stats, format_stats
                stats = get_shade_stats(common.STATE_DIR)
                if stats:
                    common.emit({'type': 'status', 'message': format_stats(stats), 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': 'No shade usage recorded yet.', 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_specialist(self, command: str, request_id: str | None):
        if command == '/specialist' or command.startswith('/specialist '):
            try:
                from charon.agents import specialists as specialists_mod
                from charon.agents import agent_lifecycle as lifecycle_mod
                rest = command[len('/specialist'):].strip()
                parts = rest.split(None, 2)
                if not parts or parts[0] == 'list':
                    lines = ['Specialist templates:']
                    for key, label in specialists_mod.list_templates().items():
                        lines.append(f'  {key}: {label}')
                    active = [a for a in lifecycle_mod.list_agents()
                              if a.get('specialization_locked')]
                    if active:
                        lines.append('Active specialists:')
                        for a in active:
                            lines.append(f"  {a.get('id')} {a.get('name', '')} "
                                         f"[{a.get('specialization', '')}] {a.get('project', '')}")
                    lines.append('Usage: /specialist create <template> [name] | '
                                 '/specialist assign <agent_id> <specialization>')
                    common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                elif parts[0] == 'create' and len(parts) >= 2:
                    a = specialists_mod.create_specialist(
                        parts[1], name=parts[2] if len(parts) > 2 else None)
                    common.emit({'type': 'status',
                          'message': f"Created specialist {a['id']} ({a['name']}) — "
                                     f"{a.get('specialization', '')}",
                          'request_id': request_id})
                elif parts[0] == 'assign' and len(parts) >= 3:
                    a = lifecycle_mod.assign_specialization(parts[1], parts[2])
                    if a:
                        common.emit({'type': 'status',
                              'message': f"{a['id']} is now: {a.get('specialization', '')}",
                              'request_id': request_id})
                    else:
                        common.emit({'type': 'error', 'error': f'Agent not found: {parts[1]}',
                              'request_id': request_id})
                else:
                    common.emit({'type': 'status',
                          'message': 'Usage: /specialist [list] | create <template> [name] | '
                                     'assign <agent_id> <specialization>',
                          'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_fleet(self, command: str, request_id: str | None):
        # /fleet — remote agent team management
        if command == '/fleet' or command.startswith('/fleet '):
            rest = command[7:].strip() if command.startswith('/fleet ') else ''

            if rest == 'status' or not rest:
                try:
                    from charon.fleet.fleet_registry import load_fleet
                    from charon.fleet.fleet_sync import get_cached_fleet_status
                    fleet = load_fleet()
                    cache = get_cached_fleet_status()
                    servers = fleet.get('servers', [])
                    if not servers:
                        common.emit({'type': 'status', 'message': 'No servers configured. Use /fleet setup <user@host> to add one.', 'request_id': request_id})
                    else:
                        for s in servers:
                            sid = s.get('id', s.get('host', '?'))
                            cached = cache.get(sid, {})
                            online = cached.get('online', False)
                            status_icon = '●' if online else '○'
                            common.emit({'type': 'status', 'message': f'  {status_icon} {sid} ({s.get("user", "")}@{s.get("host", "")})', 'request_id': request_id})
                            for a in s.get('agents', []):
                                sessions = cached.get('sessions', {})
                                agent_status = sessions.get(a['name'], {}).get('status', 'unknown')
                                common.emit({'type': 'status', 'message': f'      {a["name"]} [{a.get("specialization", "")}] — {agent_status}', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Fleet status failed: {e}', 'request_id': request_id})
                return

            if rest == 'setup' or rest.startswith('setup '):
                target = rest[6:].strip() if rest.startswith('setup ') else ''
                if not target:
                    common.emit({'type': 'status', 'message': 'Enter the server address (user@host):', 'request_id': request_id})
                    self._pending_fleet_setup = {'step': 'enter_host', 'request_id': request_id}
                    return
                # Parse user@host
                if '@' in target:
                    user, host = target.rsplit('@', 1)
                else:
                    user, host = '', target
                self._start_fleet_setup(host, user, request_id)
                return

            common.emit({'type': 'error', 'error': 'Usage: /fleet setup <user@host> or /fleet status', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_voyage(self, command: str, request_id: str | None):
        # /voyage — Harbor protocol: dispatch tasks to remote agent workers
        if command == '/voyage' or command.startswith('/voyage '):
            rest = command[8:].strip() if command.startswith('/voyage ') else ''

            if rest == 'list' or not rest:
                from charon.fleet.harbor import list_voyages
                voyages = list_voyages(common.STATE_DIR)
                if not voyages:
                    common.emit({'type': 'status', 'message': 'No voyages. Use /voyage dispatch <server> <agent> <instruction>', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': '═══ Voyages ═══', 'request_id': request_id})
                    for v in voyages:
                        status = v.get('status', '?')
                        marker = '[~]' if status in ('started', 'running', 'dispatching') else '[x]' if status == 'completed' else '[!]' if status == 'failed' else '[ ]'
                        line = f'  {marker} {v["voyage_id"]}  {v["server"]}:{v["agent"]}  {v["instruction"][:50]}'
                        common.emit({'type': 'status', 'message': line, 'request_id': request_id})
                return

            if rest.startswith('status '):
                vid = rest[7:].strip()
                from charon.fleet.harbor import get_voyage_status
                v = get_voyage_status(vid, common.STATE_DIR)
                if not v:
                    common.emit({'type': 'error', 'error': f'Voyage not found: {vid}', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': f'Voyage: {v.get("voyage_id")}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'Status: {v.get("status")}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'Server: {v.get("manifest", {}).get("server_id", "?")}:{v.get("manifest", {}).get("agent_name", "?")}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'Instruction: {v.get("manifest", {}).get("instruction", "")[:100]}', 'request_id': request_id})
                    for p in v.get('progress', [])[-5:]:
                        common.emit({'type': 'status', 'message': f'  {p.get("step", "")}: {p.get("summary", "")}', 'request_id': request_id})
                    result = v.get('result', {})
                    if result:
                        common.emit({'type': 'status', 'message': f'Return code: {result.get("returncode", "?")}', 'request_id': request_id})
                        stdout = result.get('stdout', '').strip()
                        if stdout:
                            for line in stdout.split('\n')[:20]:
                                common.emit({'type': 'status', 'message': f'  {line}', 'request_id': request_id})
                return

            if rest.startswith('dispatch '):
                parts = rest[9:].strip().split(None, 2)
                if len(parts) < 3:
                    common.emit({'type': 'error', 'error': 'Usage: /voyage dispatch <server_id> <agent_name> <instruction>', 'request_id': request_id})
                    return
                server_id, agent_name, instruction = parts[0], parts[1], parts[2]

                import threading
                def _run_dispatch():
                    from charon.fleet.harbor import dispatch_voyage
                    vid = dispatch_voyage(
                        instruction=instruction,
                        server_id=server_id,
                        agent_name=agent_name,
                        project_root=common.ROOT,
                        state_dir=common.STATE_DIR,
                        on_status=lambda msg: common.emit({'type': 'status', 'message': msg, 'request_id': request_id}),
                    )
                    if not vid:
                        common.emit({'type': 'error', 'error': 'Dispatch failed', 'request_id': request_id})

                threading.Thread(target=_run_dispatch, daemon=True).start()
                return

            common.emit({'type': 'error', 'error': 'Usage: /voyage dispatch|status|list', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_harvest_souls(self, command: str, request_id: str | None):
        # /harvest_souls — scan sibling agent repos and interactively adopt abilities
        if command == '/harvest_souls' or command.startswith('/harvest_souls '):
            rest = command[len('/harvest_souls '):].strip() if command.startswith('/harvest_souls ') else ''

            if rest == 'status':
                from charon.memory.assimilation import load_last_scan
                scan = load_last_scan(common.STATE_DIR)
                if not scan:
                    common.emit({'type': 'status', 'message': 'No scan found. Run /harvest_souls to scan agent repos.', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': f'Last scan: {scan.get("timestamp", "?")}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'Repos: {", ".join(scan.get("repos_scanned", []))} | Unavailable: {", ".join(scan.get("repos_unavailable", []))}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'Tools: {scan.get("total_tools", 0)} | Skills: {scan.get("total_skills", 0)} | Commands: {scan.get("total_commands", 0)}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'New abilities: {scan.get("new_abilities", 0)}', 'request_id': request_id})
                    adopted_file = common.STATE_DIR / 'assimilation' / 'adopted.json'
                    if adopted_file.exists():
                        try:
                            adopted = json.loads(adopted_file.read_text())
                            common.emit({'type': 'status', 'message': f'Adopted: {len(adopted)} abilities', 'request_id': request_id})
                        except Exception as exc:
                            _diag('commands_mixin', 'adopted.json unreadable; harvest status omits adopted count', error=exc)
                return

            # /harvest_souls list — show numbered findings from last scan
            if rest == 'list':
                self._harvest_souls_show_findings(request_id)
                return

            # /harvest_souls evaluate — run capability-level gap evaluation from last scan
            if rest == 'evaluate':
                import threading
                def _run_gap_eval():
                    try:
                        from charon.memory.assimilation import run_gap_evaluation_from_saved_scan
                        clusters = run_gap_evaluation_from_saved_scan(
                            state_dir=common.STATE_DIR,
                            docs_dir=common.ROOT / 'docs',
                            charon_root=common.ROOT,
                            on_status=lambda msg: common.emit({'type': 'status', 'message': msg, 'request_id': request_id}),
                        )
                        if not clusters:
                            common.emit({'type': 'status', 'message': 'No saved scan found. Run /harvest_souls first.', 'request_id': request_id})
                        else:
                            common.emit({'type': 'status', 'message': f'Gap evaluation complete — {len(clusters)} capability clusters.', 'request_id': request_id})
                            self._harvest_souls_review(request_id)
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Gap evaluation failed: {e}', 'request_id': request_id})
                common.emit({'type': 'status', 'message': 'Evaluating capability gaps from last harvest scan...', 'request_id': request_id})
                threading.Thread(target=_run_gap_eval, daemon=True).start()
                return

            # /harvest_souls review — show capability-level harvest menu
            if rest == 'review':
                self._harvest_souls_review(request_id)
                return

            # /harvest_souls decide <N> — inspect capability-level decision
            if rest.startswith('decide '):
                idx_str = rest[7:].strip()
                self._harvest_souls_decide(idx_str, request_id)
                return

            # /harvest_souls harvest <N|N,N,N|all> — queue capability clusters for assimilation
            if rest.startswith('harvest '):
                selection = rest[8:].strip()
                self._harvest_souls_harvest(selection, request_id)
                return

            # /harvest_souls plan <N> — show implementation path for ability #N
            if rest.startswith('plan '):
                idx_str = rest[5:].strip()
                self._harvest_souls_plan(idx_str, request_id)
                return

            # /harvest_souls adopt <N|N,N,N|all> — mark abilities for adoption
            if rest.startswith('adopt '):
                selection = rest[6:].strip()
                self._harvest_souls_adopt(selection, request_id)
                return

            # /harvest_souls roadmap — show the full adoption roadmap
            if rest == 'roadmap':
                self._harvest_souls_roadmap(request_id)
                return

            # Default: run the scan, then show findings
            agent_filter = rest if rest and rest not in ('list', 'status', 'roadmap') else None

            import threading
            def _run_assimilation():
                try:
                    from charon.memory.assimilation import run_full_assimilation
                    result = run_full_assimilation(
                        state_dir=common.STATE_DIR,
                        docs_dir=common.ROOT / 'docs',
                        charon_root=common.ROOT,
                        agent_filter=agent_filter,
                        on_status=lambda msg: common.emit({'type': 'status', 'message': msg, 'request_id': request_id}),
                    )
                    common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': f'Scan complete — {result.get("new_abilities", 0)} new abilities, {result.get("real_gaps", 0)} real capability gaps.', 'request_id': request_id})
                    # Show capability review inline when available; fall back to raw findings
                    self._harvest_souls_review(request_id)
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Harvest failed: {e}', 'request_id': request_id})

            common.emit({'type': 'status', 'message': f'Scanning agent repos{f" ({agent_filter})" if agent_filter else ""}...', 'request_id': request_id})
            threading.Thread(target=_run_assimilation, daemon=True).start()
            return
        return UNHANDLED
