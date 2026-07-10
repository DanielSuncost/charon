"""Dashboard data helpers and the refresh-payload mixin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend import common
from backend.settings_io import _load_project_registry
from charon.providers.provider_bridge import load_session_provider_config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _collect_devop_rooms(state_dir: Path, project_root: Path) -> list[dict]:
    rooms = []
    try:
        from charon.devop.devop_runtime import software_ops_root, get_operation_state
        from charon.devop.devop_projection import project_graph, project_f4_stream, summarize_operation

        ops_dir = software_ops_root(state_dir)
        if not ops_dir.exists():
            return []
        wanted_root = str(project_root.resolve())
        for op_path in sorted(ops_dir.glob('*')):
            if not op_path.is_dir():
                continue
            op = get_operation_state(state_dir, op_path.name)
            if not op:
                continue
            op_root = str(op.get('project_root') or '').strip()
            if op_root and op_root != wanted_root:
                continue
            op_id = str(op.get('operation_id') or '').strip()
            if not op_id:
                continue
            graph = project_graph(state_dir, op_id)
            f4 = project_f4_stream(state_dir, op_id)
            summary = summarize_operation(state_dir, op_id)
            rooms.append({
                'id': f'devop-{op_id}',
                'kind': 'software_dev',
                'title': str(op.get('title') or op.get('prompt') or op_id)[:120],
                'project': str(op.get('project_root') or project_root),
                'status': str(op.get('status') or 'active'),
                'created_at': str(op.get('created_at') or ''),
                'updated_at': str(op.get('updated_at') or ''),
                'last_activity': str((summary.get('last_event') or {}).get('ts') or op.get('updated_at') or op.get('created_at') or ''),
                'participants': [
                    {
                        'id': str(n.get('id') or ''),
                        'name': str(n.get('label') or n.get('id') or ''),
                        'role': str(n.get('operation_role') or n.get('node_type') or ''),
                        'status': str(n.get('status') or ''),
                    }
                    for n in (graph.get('nodes') or []) if str(n.get('node_type') or '') == 'agent'
                ],
                'summary': str(op.get('prompt') or '')[:200],
                'operation_id': op_id,
                'domain': 'software_dev',
                'nodes': graph.get('nodes') or [],
                'edges': graph.get('edges') or [],
                'workstreams': f4.get('workstreams') or [],
                'active_reviews': f4.get('active_reviews') or [],
                'events': f4.get('stream') or [],
            })
    except Exception as e:
        _diag('dashboard', 'devop room collection failed; software-dev rooms omitted from dashboard', error=e)
        return []
    return rooms


def _dashboard_spark_points(values: list[int], limit: int = 12) -> list[int]:
    vals = [max(0, int(v or 0)) for v in values][-limit:]
    return vals or [0]


def _load_workflow_steps_spec(project_root: Path, raw_value: str) -> list[dict] | None:
    raw = str(raw_value or '').strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else None
    except Exception as e:
        _diag('dashboard', 'workflow steps file unreadable; workflow spec ignored', error=e)
        return None
    return None


def _project_goal_tree(state_dir: Path, project_path: str) -> list[dict]:
    try:
        from charon.agents.goal_runtime import list_goals
        goals = list_goals(state_dir, project=project_path)
    except Exception as e:
        _diag('dashboard', 'goal listing unavailable; project goal tree empty', error=e, project=project_path)
        goals = []
    if not goals:
        return []
    by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for g in goals:
        pid = str(g.get('parent_goal_id') or '')
        by_parent.setdefault(pid, []).append(g)
    def build(node: dict) -> dict:
        gid = str(node.get('goal_id') or '')
        children = [build(c) for c in by_parent.get(gid, [])]
        return {
            'goal_id': gid,
            'title': str(node.get('title') or ''),
            'status': str(node.get('status') or ''),
            'children': children,
        }
    roots = [build(g) for g in by_parent.get('', [])]
    if not roots:
        roots = [build(g) for g in goals[:20]]
    return roots[:20]


def _project_usage_summary(state_dir: Path, project_path: str) -> dict:
    summary = {
        'input_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'estimated_cost_usd': 0.0,
        'hours_spent_estimate': 0.0,
        'libris_operations': 0,
        'devop_operations': 0,
    }
    try:
        from charon.libris.libris_runtime import research_root
        rroot = research_root(state_dir, Path(project_path))
        ops_dir = rroot / 'operations'
        if ops_dir.exists():
            for op_path in ops_dir.glob('*'):
                op = common._load_json(op_path / 'operation.json', {})
                if not op:
                    continue
                summary['libris_operations'] += 1
                usage = op.get('usage') or {}
                summary['input_tokens'] += int(usage.get('input_tokens') or 0)
                summary['output_tokens'] += int(usage.get('output_tokens') or 0)
                summary['total_tokens'] += int(usage.get('total_tokens') or 0)
                summary['estimated_cost_usd'] += float(usage.get('estimated_cost_usd') or 0.0)
    except Exception as e:
        _diag('dashboard', 'libris usage scan failed; project usage summary incomplete', error=e)
    try:
        pass  # type: ignore
    except Exception:
        pass
    try:
        from charon.devop.devop_runtime import software_ops_root, get_operation_state
        for op_path in (software_ops_root(state_dir)).glob('*'):
            if not op_path.is_dir():
                continue
            op = get_operation_state(state_dir, op_path.name)
            if not op or str(op.get('project_root') or '').strip() != str(Path(project_path).resolve()):
                continue
            summary['devop_operations'] += 1
            usage = op.get('usage') or {}
            summary['input_tokens'] += int(usage.get('input_tokens') or 0)
            summary['output_tokens'] += int(usage.get('output_tokens') or 0)
            summary['total_tokens'] += int(usage.get('total_tokens') or 0)
            summary['estimated_cost_usd'] += float(usage.get('estimated_cost_usd') or 0.0)
    except Exception as e:
        _diag('dashboard', 'devop usage scan failed; project usage summary incomplete', error=e)
    summary['estimated_cost_usd'] = round(float(summary['estimated_cost_usd']), 6)
    summary['hours_spent_estimate'] = round((summary['total_tokens'] / 12000.0), 2) if summary['total_tokens'] else 0.0
    return summary


def _project_activity_points(state_dir: Path, project_path: str) -> list[int]:
    counts = [0] * 12
    try:
        run_log = state_dir / 'run.log'
        if run_log.exists():
            lines = run_log.read_text(encoding='utf-8', errors='replace').splitlines()[-240:]
            for i, _line in enumerate(lines[-12:]):
                counts[min(11, i)] += 1
    except Exception as e:
        _diag('dashboard', 'run.log read failed; project activity sparkline empty', error=e)
    return _dashboard_spark_points(counts)


class DashboardMixin:
    """Refresh payload, dashboard, and session-info handlers."""

    def _get_refresh_payload(self) -> dict:
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        session_id = self._active_agent_id or None
        session_override = load_session_provider_config(common.STATE_DIR, session_id) if session_id else {}
        effective_onboarding = dict(onboarding)
        if session_override:
            effective_onboarding.update(session_override)

        session_cfg = self._session_provider_state()
        provider = str(session_cfg.get('provider_raw') or effective_onboarding.get('provider') or '').strip()
        model = str(session_cfg.get('model_id') or effective_onboarding.get('model') or effective_onboarding.get('provider_model') or '').strip()
        complete = bool(session_cfg.get('ready') or effective_onboarding.get('complete'))
        if self.engine is not None:
            provider = str(getattr(self.engine, 'provider_name', '') or provider).strip()
            model = str(getattr(getattr(self.engine, 'model', None), 'model_id', '') or model).strip()
            complete = True

        # Load agents
        agents = []
        try:
            from charon.agents.agent_lifecycle import list_agents
            for a in list_agents():
                agent_id = a.get('id', '')
                # Load recent actions from inbox
                recent_actions = []
                inbox_path = common.STATE_DIR / 'agents' / agent_id / 'inbox.jsonl'
                if inbox_path.exists():
                    try:
                        inbox_lines = inbox_path.read_text().splitlines()[-8:]
                        for line in inbox_lines:
                            try:
                                rec = json.loads(line)
                                evt = rec.get('event_type', '')
                                payload = rec.get('payload', {})
                                summary = payload.get('summary', payload.get('instruction', ''))
                                if summary:
                                    recent_actions.append(f"{evt}: {str(summary)[:60]}")
                                elif evt:
                                    recent_actions.append(evt)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # Load working memory for goal info
                memory_path = common.STATE_DIR / 'agents' / agent_id / 'working_memory.json'
                memory = common._load_json(memory_path, {})
                last_summary = memory.get('last_task_summary', '')
                notes = memory.get('notes', [])

                agents.append({
                    'id': agent_id,
                    'name': a.get('name', ''),
                    'status': a.get('status', 'idle'),
                    'role': a.get('role', 'charon'),
                    'goal': a.get('goal', ''),
                    'specialization': a.get('specialization', ''),
                    'project': a.get('project', ''),
                    'mode': a.get('mode', 'persistent'),
                    'visibility': a.get('visibility', 'user'),
                    'last_active': a.get('last_active', ''),
                    'parent_agent_id': a.get('parent_agent_id', ''),
                    'tmux_session': a.get('tmux_session', ''),
                    'recent_actions': recent_actions,
                    'last_summary': str(last_summary)[:120] if last_summary else '',
                    'memory_notes': len(notes),
                })

                # Add ledger entries for rear-view
                try:
                    from charon.agents.task_ledger import get_agent_ledger
                    ledger = get_agent_ledger(common.STATE_DIR, agent_id, limit=10)
                    agents[-1]['ledger'] = ledger
                except Exception:
                    agents[-1]['ledger'] = []

                # Add shade usage stats
                try:
                    from charon.shade.shade_stats import get_agent_shade_stats
                    agents[-1]['shade_stats'] = get_agent_shade_stats(common.STATE_DIR, agent_id)
                except Exception:
                    agents[-1]['shade_stats'] = {}
        except Exception as e:
            _diag('dashboard', 'agent listing failed; dashboard shows no local agents', error=e)

        # Remote fleet agents
        try:
            from charon.fleet.fleet_registry import load_fleet
            from charon.fleet.fleet_sync import get_cached_fleet_status
            fleet = load_fleet()
            fleet_status = get_cached_fleet_status()
            for server in fleet.get('servers', []):
                server_id = server.get('id', server.get('host', ''))
                server_info = fleet_status.get(server_id, {})
                server_sessions = server_info.get('sessions', {})
                for agent_cfg in server.get('agents', []):
                    agent_name = agent_cfg.get('name', '')
                    session_info = server_sessions.get(agent_name, {})
                    remote_status = session_info.get('status', 'offline') if server_info.get('online') else 'offline'
                    agents.append({
                        'id': f"remote:{server_id}:{agent_name}",
                        'name': agent_name,
                        'status': remote_status,
                        'role': agent_cfg.get('type', 'remote'),
                        'goal': '',
                        'specialization': agent_cfg.get('specialization', ''),
                        'project': agent_cfg.get('project', ''),
                        'mode': 'persistent',
                        'visibility': 'user',
                        'last_active': '',
                        'parent_agent_id': '',
                        'tmux_session': session_info.get('session_id', ''),
                        'recent_actions': [],
                        'last_summary': '',
                        'memory_notes': 0,
                        'is_remote': True,
                        'server_id': server_id,
                        'host': server.get('host', ''),
                        'transport': 'remote-boat',
                    })
        except Exception as e:
            _diag('dashboard', 'fleet registry/status unavailable; remote fleet agents omitted', error=e)

        # Derive projects from agents
        project_map: dict[str, dict] = {}
        for a in agents:
            proj = a.get('project', '').strip()
            if not proj:
                continue
            name = proj.split('/')[-1] or proj
            if name not in project_map:
                project_map[name] = {
                    'name': name,
                    'path': proj,
                    'agents': [],
                    'agent_details': [],
                    'last_active': '',
                    'started': '',
                }
            project_map[name]['agents'].append(a.get('name', a.get('id', '')))
            project_map[name]['agent_details'].append({
                'name': a.get('name', ''),
                'id': a.get('id', ''),
                'status': a.get('status', 'idle'),
                'role': a.get('role', 'charon'),
            })
            ts = a.get('last_active', '')
            if ts > project_map[name].get('last_active', ''):
                project_map[name]['last_active'] = ts
            created = a.get('created_at', '')
            if not project_map[name]['started'] or (created and created < project_map[name]['started']):
                project_map[name]['started'] = created
        projects = list(project_map.values())

        # Merge explicit project objects from registry
        try:
            registry = _load_project_registry()
            by_name = {p.get('name', ''): p for p in projects}
            for entry in registry:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('name') or '').strip()
                path = str(entry.get('path') or '').strip()
                if not name:
                    continue
                proj = by_name.get(name)
                if proj is None:
                    proj = {
                        'name': name,
                        'path': path,
                        'agents': [],
                        'agent_details': [],
                        'last_active': '',
                        'started': str(entry.get('created_at') or ''),
                        'active': False,
                        'explicit': True,
                        'description': str(entry.get('description') or ''),
                    }
                    projects.append(proj)
                    by_name[name] = proj
                else:
                    proj['explicit'] = True
                    if path and not proj.get('path'):
                        proj['path'] = path
                    if entry.get('description') and not proj.get('description'):
                        proj['description'] = str(entry.get('description') or '')
            onboarding_project = str(onboarding.get('project') or '').strip()
            for p in projects:
                p['active'] = any(ad.get('status') == 'running' for ad in p.get('agent_details', []))
                p['selected'] = bool(onboarding_project and str(p.get('path') or '').strip() == onboarding_project)
        except Exception as e:
            _diag('dashboard', 'project registry merge failed; explicit projects omitted', error=e)
            for p in projects:
                p['active'] = any(ad.get('status') == 'running' for ad in p.get('agent_details', []))

        for p in projects:
            path = str(p.get('path') or '').strip()
            usage = _project_usage_summary(common.STATE_DIR, path or str(common.ROOT))
            goal_tree = _project_goal_tree(common.STATE_DIR, path or str(common.ROOT))
            flat_goals = []
            try:
                from charon.agents.goal_runtime import list_goals
                flat_goals = list_goals(common.STATE_DIR, project=path or str(common.ROOT))
            except Exception as e:
                _diag('dashboard', 'goal listing unavailable; project goal counts zeroed', error=e, project=path)
                flat_goals = []
            p['usage'] = usage
            p['goal_tree'] = goal_tree
            p['goal_counts'] = {
                'total': len(flat_goals),
                'completed': sum(1 for g in flat_goals if str(g.get('status') or '') == 'completed'),
                'active': sum(1 for g in flat_goals if str(g.get('status') or '') in {'active', 'executing', 'planning', 'verifying'}),
                'pending': sum(1 for g in flat_goals if str(g.get('status') or '') in {'backlog', 'proposed', 'confirmed'}),
                'blocked': sum(1 for g in flat_goals if str(g.get('status') or '') == 'blocked'),
            }
            p['activity_points'] = _project_activity_points(common.STATE_DIR, path or str(common.ROOT))

        # Derive sessions — discover ALL tmux sessions, match to agents where possible
        sessions = []
        live_tmux: dict[str, dict] = {}
        claimed_tmux: set[str] = set()
        try:
            from charon.fleet.tmux_capture import list_sessions as tmux_list
            for ts in tmux_list():
                live_tmux[ts.name] = {
                    'name': ts.name,
                    'windows': ts.windows,
                    'attached': ts.attached,
                }
        except Exception as e:
            _diag('dashboard', 'tmux session listing failed; live tmux sessions omitted', error=e)

        # First: add Charon agents that have tmux sessions
        for a in agents:
            tmux_name = a.get('tmux_session', '')
            has_tmux = tmux_name in live_tmux
            if tmux_name:
                claimed_tmux.add(tmux_name)
            sessions.append({
                'id': f"session-{a['id']}",
                'agentId': a['id'],
                'agentName': a['name'],
                'sessionLabel': a['name'],
                'status': a['status'] if has_tmux else 'stopped',
                'project': a['project'].split('/')[-1] if a.get('project') else '',
                'location': 'local',
                'lastActivity': a.get('last_active', ''),
                'tmuxSession': tmux_name,
                'tmux_session': tmux_name,
                'hasTmux': has_tmux,
                'role': a.get('role', 'charon'),
                'source': 'charon',
            })

        # Boat-wrapped sessions (fast path for Hermes/Pi demo sessions)
        try:
            boat_dir = Path.home() / '.charon' / 'boats'
            if boat_dir.exists():
                for reg_file in sorted(boat_dir.glob('*.json')):
                    try:
                        reg = json.loads(reg_file.read_text())
                    except Exception:
                        continue
                    tmux_name = str(reg.get('session') or '').strip()
                    if not tmux_name:
                        continue
                    transport = str(reg.get('transport') or '').strip().lower()
                    reg_status = str(reg.get('status') or 'idle').strip() or 'idle'
                    has_tmux = tmux_name in live_tmux
                    sock_path = Path(str(reg.get('socket') or ''))
                    if transport in ('pty', 'charon'):
                        if reg_status not in ('running', 'starting') or not sock_path.exists():
                            continue
                    elif not has_tmux:
                        continue
                    if has_tmux and tmux_name in claimed_tmux:
                        continue
                    raw_name = str(reg.get('name') or tmux_name).strip() or tmux_name
                    command = str(reg.get('command') or '').strip()
                    base = command.split()[0] if command else raw_name
                    agent_target = Path(base).name.lower()
                    if agent_target.startswith('boat-'):
                        agent_target = raw_name.lower()
                    if transport == 'charon' or 'charon' in agent_target:
                        agent_name = 'Charon'
                        process_target = 'charon'
                    elif 'hermes' in agent_target:
                        agent_name = 'Hermes'
                        process_target = 'hermes'
                    elif agent_target == 'pi':
                        agent_name = 'Pi'
                        process_target = 'pi'
                    else:
                        agent_name = raw_name.split('-')[0].capitalize() or 'Agent'
                        process_target = agent_target or 'external'
                    if has_tmux:
                        claimed_tmux.add(tmux_name)
                    session_label = raw_name
                    if transport == 'charon' and raw_name and not raw_name.startswith('charon'):
                        session_label = f'charon-{raw_name}'
                    sessions.append({
                        'id': f'boat-{tmux_name}',
                        'agentId': f'boat-{tmux_name}',
                        'agentName': agent_name,
                        'sessionLabel': session_label,
                        'status': 'running' if has_tmux else reg_status,
                        'project': '',
                        'location': 'local',
                        'lastActivity': str(reg.get('created') or ''),
                        'tmuxSession': tmux_name,
                        'tmux_session': tmux_name,
                        'hasTmux': has_tmux,
                        'role': 'external',
                        'source': 'boat',
                        'processTarget': process_target,
                        'hasBoat': True,
                        'supportsCharonBoat': True,
                        'boatSessionId': raw_name,
                        'command': command[:80],
                        'transport': transport or ('pty' if sock_path.exists() else ''),
                        'socket': str(sock_path) if sock_path else '',
                    })
        except Exception as e:
            _diag('dashboard', 'boat registry scan failed; boat sessions omitted', error=e)

        # Remote fleet agent sessions
        try:
            from charon.fleet.fleet_registry import load_fleet as _fleet_load
            from charon.fleet.fleet_sync import get_cached_fleet_status as _fleet_status
            _fleet = _fleet_load()
            _fstatus = _fleet_status()
            for _srv in _fleet.get('servers', []):
                _sid = _srv.get('id', _srv.get('host', ''))
                _sinfo = _fstatus.get(_sid, {})
                _ssessions = _sinfo.get('sessions', {})
                for _acfg in _srv.get('agents', []):
                    _aname = _acfg.get('name', '')
                    _sess_info = _ssessions.get(_aname, {})
                    _rstatus = _sess_info.get('status', 'offline') if _sinfo.get('online') else 'offline'
                    _remote_id = f"remote:{_sid}:{_aname}"
                    sessions.append({
                        'id': _remote_id,
                        'agentId': _remote_id,
                        'agentName': _aname,
                        'sessionLabel': f'{_aname} @ {_sid}',
                        'status': _rstatus,
                        'project': _acfg.get('project', '').split('/')[-1] if _acfg.get('project') else '',
                        'location': _srv.get('host', ''),
                        'lastActivity': '',
                        'tmuxSession': _sess_info.get('session_id', _aname),
                        'tmux_session': _sess_info.get('session_id', _aname),
                        'hasTmux': _rstatus in ('running', 'idle'),
                        'role': _acfg.get('type', 'remote'),
                        'source': 'fleet',
                        'transport': 'remote-boat',
                        'server_id': _sid,
                        'socket': '',
                    })
        except Exception as e:
            _diag('dashboard', 'fleet registry/status unavailable; remote fleet sessions omitted', error=e)

        # Second: discover ALL running agent processes (pi, hermes, claude, etc.)
        # and match them to tmux sessions where possible
        detected_agents: list[dict] = []
        tmux_pane_map: dict[int, str] = {}  # pid → tmux session name
        try:
            import subprocess as _sp
            # Get tmux pane PIDs and commands to match processes to sessions
            result = _sp.run(
                ['tmux', 'list-panes', '-a', '-F', '#{session_name} #{pane_pid} #{pane_current_command}'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                pane_pids: list[tuple[int, str]] = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split(None, 2)
                    if len(parts) >= 2:
                        try:
                            pane_pids.append((int(parts[1]), parts[0]))
                            tmux_pane_map[int(parts[1])] = parts[0]
                        except ValueError:
                            pass
                # Map child and grandchild PIDs to their pane's tmux session
                for pane_pid, sess_name in pane_pids:
                    try:
                        cr = _sp.run(['pgrep', '-P', str(pane_pid)], capture_output=True, text=True, timeout=3)
                        if cr.returncode == 0:
                            for cl in cr.stdout.strip().splitlines():
                                try:
                                    cpid = int(cl.strip())
                                    tmux_pane_map[cpid] = sess_name
                                    # Grandchildren
                                    gr = _sp.run(['pgrep', '-P', str(cpid)], capture_output=True, text=True, timeout=3)
                                    if gr.returncode == 0:
                                        for gl in gr.stdout.strip().splitlines():
                                            try:
                                                tmux_pane_map[int(gl.strip())] = sess_name
                                            except ValueError:
                                                pass
                                except ValueError:
                                    pass
                    except Exception:
                        pass
        except Exception as e:
            _diag('dashboard', 'tmux pane mapping failed; detected processes lack session names', error=e)

        try:
            sys.path.insert(0, str(common.ROOT / 'apps' / 'tui'))
            from process_inspector import detect_agent_processes
            for proc in detect_agent_processes():
                # Skip if this PID belongs to a Charon agent tmux we already listed
                tmux_session = tmux_pane_map.get(proc.pid, '')
                if tmux_session in claimed_tmux:
                    continue
                agent_name = f"{proc.target}"
                if tmux_session:
                    agent_name = f"{proc.target} ({tmux_session})"
                    claimed_tmux.add(tmux_session)
                detected_agents.append({
                    'id': f"proc-{proc.pid}",
                    'agentId': f"proc-{proc.pid}",
                    'agentName': agent_name,
                    'status': 'running',
                    'project': '',
                    'location': 'local',
                    'lastActivity': '',
                    'tmuxSession': tmux_session,
                    'tmux_session': tmux_session,
                    'hasTmux': bool(tmux_session),
                    'role': 'external',
                    'source': 'detected',
                    'processTarget': proc.target,
                    'hasBoat': bool(getattr(proc, 'has_boat', False)),
                    'supportsCharonBoat': bool(getattr(proc, 'has_boat', False)),
                    'pid': proc.pid,
                    'command': proc.args[:80],
                })
        except Exception as e:
            _diag('dashboard', 'agent process detection failed; detected sessions omitted', error=e)

        sessions.extend(detected_agents)

        # Also add Charon agents as virtual sessions (viewable in grid as chat history)
        for a in agents:
            if a.get('role') != 'charon':
                continue
            aid = a['id']
            if any(s.get('agentId') == aid for s in sessions):
                continue  # already has a session
            sessions.append({
                'id': f"virtual-{aid}",
                'agentId': aid,
                'agentName': a['name'],
                'status': a['status'],
                'project': a['project'].split('/')[-1] if a.get('project') else '',
                'location': 'local',
                'lastActivity': a.get('last_active', ''),
                'tmuxSession': '',
                'tmux_session': '',
                'hasTmux': False,
                'role': 'charon',
                'source': 'virtual',
                'isVirtual': True,
            })

        # Third: add any remaining tmux sessions not claimed by agents or detected processes
        for tmux_name, _tmux_info in live_tmux.items():
            if tmux_name in claimed_tmux:
                continue
            sessions.append({
                'id': f"tmux-{tmux_name}",
                'agentId': f"tmux-{tmux_name}",
                'agentName': f"tmux:{tmux_name}",
                'status': 'running',
                'project': '',
                'location': 'local',
                'lastActivity': '',
                'tmuxSession': tmux_name,
                'tmux_session': tmux_name,
                'hasTmux': True,
                'role': 'external',
                'source': 'tmux',
            })

        # Recent activity from run log
        activity = []
        run_log = common.STATE_DIR / 'run.log'
        if run_log.exists():
            try:
                lines = run_log.read_text().splitlines()[-15:]
                for line in lines:
                    try:
                        rec = json.loads(line)
                        evt = rec.get('event', '?')
                        tid = rec.get('task_id', '')
                        reason = rec.get('reason', '')
                        activity.append(f"{evt}: {tid} {reason}".strip())
                    except Exception:
                        pass
            except Exception as e:
                _diag('dashboard', 'run.log read failed; recent activity empty', error=e)

        transfer_events = []
        try:
            from charon.context.context_transfer import list_transfer_events
            transfer_events = list_transfer_events(common.STATE_DIR, limit=12)
        except Exception as e:
            _diag('dashboard', 'transfer event listing failed; transfer events empty', error=e)
            transfer_events = []

        inter_agent_rooms = []
        try:
            from charon.agents.inter_agent_rooms import list_rooms, list_events
            for room in list_rooms(common.STATE_DIR, limit=40):
                rid = str(room.get('id') or '')
                if not rid:
                    continue
                item = dict(room)
                item['events'] = list_events(common.STATE_DIR, rid, limit=80)
                inter_agent_rooms.append(item)
        except Exception as e:
            _diag('dashboard', 'inter-agent room listing failed; rooms omitted', error=e)
            inter_agent_rooms = []

        # Map Libris and software-dev operations into the shared F4 room list so
        # F4 can render them with a graph-first layout later.
        project_root = Path(str(onboarding.get('project') or str(common.ROOT)).strip() or str(common.ROOT))
        try:
            from charon.libris.libris_runtime import rebuild_project_index, get_libris_swarm_state
            idx = rebuild_project_index(common.STATE_DIR, project_root)
            for op in idx.get('operations') or []:
                op_id = str(op.get('operation_id') or '').strip()
                if not op_id:
                    continue
                swarm = get_libris_swarm_state(common.STATE_DIR, project_root, op_id)
                if not swarm:
                    continue
                inter_agent_rooms.append({
                    'id': f'libris-{op_id}',
                    'kind': 'libris',
                    'title': str(op.get('prompt') or op_id)[:120],
                    'project': str(project_root),
                    'status': str(swarm.get('status') or op.get('status') or 'active'),
                    'created_at': str(op.get('created_at') or ''),
                    'updated_at': str(op.get('updated_at') or ''),
                    'last_activity': str(op.get('updated_at') or op.get('created_at') or ''),
                    'participants': [
                        {
                            'id': str(n.get('agent_id') or ''),
                            'name': str(n.get('name') or ''),
                            'role': str(n.get('role') or ''),
                            'status': str(n.get('status') or ''),
                        }
                        for n in (swarm.get('nodes') or [])
                    ],
                    'summary': str(swarm.get('prompt') or '')[:200],
                    'operation_id': op_id,
                    'nodes': swarm.get('nodes') or [],
                    'edges': swarm.get('edges') or [],
                    'topics': swarm.get('topics') or [],
                    'team_grid_nodes': swarm.get('team_grid_nodes') or [],
                    'non_shade_members': swarm.get('non_shade_members') or [],
                    'views': swarm.get('views') or {},
                    'counts': swarm.get('counts') or {},
                    'budget_status': swarm.get('budget_status') or {},
                    'promising_sources': swarm.get('promising_sources') or [],
                    'executive_summary_markdown': swarm.get('executive_summary_markdown') or '',
                    'delivery_bundle': swarm.get('delivery_bundle') or {},
                    'final_selection_markdown': swarm.get('final_selection_markdown') or '',
                    'events': swarm.get('events_tail') or [],
                })
        except Exception as e:
            _diag('dashboard', 'libris swarm indexing failed; libris rooms omitted', error=e)

        try:
            inter_agent_rooms.extend(_collect_devop_rooms(common.STATE_DIR, project_root))
        except Exception as e:
            _diag('dashboard', 'devop room merge failed; software-dev rooms omitted', error=e)

        session_lookup = {}
        for s in sessions:
            sid = str(s.get('tmuxSession') or s.get('tmux_session') or s.get('id') or '').strip()
            if sid:
                session_lookup[sid] = s
            raw_boat = str(s.get('boatSessionId') or '').strip()
            if raw_boat:
                session_lookup[raw_boat if raw_boat.startswith('boat-') else f'boat-{raw_boat}'] = s
        for room in inter_agent_rooms:
            participant_sessions = room.get('participant_sessions') or []
            participants = room.get('participants') or []
            if not participant_sessions and participants:
                participant_sessions = [p.get('session') for p in participants if p.get('session')]
                room['participant_sessions'] = participant_sessions
            room['session_details'] = [session_lookup[s] for s in participant_sessions if s in session_lookup]

        automations = []
        try:
            from charon.automation.automation_runtime import list_automations, get_automation_state
            automations = [get_automation_state(common.STATE_DIR, str(a.get('automation_id') or '')) for a in list_automations(common.STATE_DIR)]
        except Exception as e:
            _diag('dashboard', 'automation listing failed; automations empty', error=e)
            automations = []

        payload = {
            'onboarding': {
                'complete': complete,
                'provider': provider,
                'model': model,
                'step': effective_onboarding.get('step', 'provider-mode'),
                'project': str(effective_onboarding.get('project') or '').strip(),
            },
            'agents': agents,
            'projects': projects,
            'sessions': sessions,
            'activity': activity,
            'transfer_events': transfer_events,
            'inter_agent_rooms': inter_agent_rooms,
            'automations': automations,
            'dashboard': {
                'agents_row': {'items': agents},
                'projects_row': {'items': projects},
                'automations_row': {'items': automations},
            },
            'chat_history': self.chat_history[-200:],
            'engine_ready': self.engine is not None,
            'message_count': len(self.engine.messages) if self.engine else 0,
            'agent_mode': self.agent_mode,
            'session_info': self._get_session_info(),
            'batch_progress': self._get_batch_progress(),
            'orchestration_parse': dict(self._last_orchestration_parse or {}),
        }

        # Include recent consolidation traces for dashboard
        try:
            from charon.memory.consolidation import list_traces
            payload['consolidation_traces'] = list_traces(common.STATE_DIR, limit=5)
        except Exception as e:
            _diag('dashboard', 'consolidation trace listing failed; traces omitted', error=e)
            payload['consolidation_traces'] = []

        return payload

    def handle_refresh(self, request_id: str | None):
        payload = self._get_refresh_payload()
        payload['session_id'] = self._active_agent_id or ''
        payload['visible_thoughts'] = self.visible_thoughts
        payload['thoughts_supported'] = self._thoughts_supported()

        # Check for incoming steering messages from other Charon instances
        if self._active_agent_id:
            try:
                from charon.agents.session_registry import read_steers
                steers = read_steers(common.STATE_DIR, self._active_agent_id)
                for steer in steers:
                    msg = steer.get('message', '')
                    if msg:
                        common.emit({
                            'type': 'status',
                            'message': f'📡 Message from another Charon: {msg[:80]}',
                            'request_id': request_id,
                        })
                        # Submit as a regular chat message so the agent responds
                        import threading
                        threading.Thread(
                            target=self.handle_chat,
                            args=(f'[Steering from another Charon session] {msg}', request_id),
                            daemon=True,
                        ).start()
            except Exception as e:
                _diag('dashboard', 'steer inbox check failed; cross-session steering messages dropped', error=e)

        # Heartbeat + include live Charon sessions
        try:
            from charon.agents.session_registry import heartbeat, list_live_sessions
            if self._active_agent_id:
                heartbeat(common.STATE_DIR, self._active_agent_id)
            live = list_live_sessions(common.STATE_DIR)
            # Add live sessions as agents (if not already present)
            for ls in live:
                sid = ls.get('session_id', '')
                if sid == self._active_agent_id:
                    continue  # skip self
                if not ls.get('alive', False):
                    continue
                # Add as a session entry
                payload.setdefault('sessions', []).append({
                    'id': f'live-{sid}',
                    'agentId': f'live-{sid}',
                    'agentName': f'charon ({sid.split("-")[-1][:6]})',
                    'status': 'running',
                    'project': '',
                    'location': 'local',
                    'lastActivity': '',
                    'tmuxSession': '',
                    'tmux_session': '',
                    'hasTmux': False,
                    'role': 'charon',
                    'source': 'live',
                    'isLive': True,
                    'liveSessionId': sid,
                })
        except Exception as e:
            _diag('dashboard', 'session heartbeat/live-session listing failed; live sessions omitted', error=e)

        common.emit({
            'type': 'refresh',
            'request_id': request_id,
            'payload': payload,
        })

    def _get_session_info(self) -> dict:
        """Build session info for the right-side pane.
        
        Three tabs:
        1. Session outcome ledger
        2. Estimated goal structure
        3. User model
        Plus token usage breakdown at the bottom.
        """
        info = {
            'tasks': [],
            'goals': [],
            'goal_summary': {
                'active_goal_id': '',
                'session_total': 0,
                'project_total': 0,
                'proposed': 0,
                'confirmed': 0,
                'executing': 0,
                'verifying': 0,
                'active': 0,
                'backlog': 0,
                'blocked': 0,
                'completed_recent': 0,
                'failed_recent': 0,
            },
            'user_model': '',
            'transfer': {},
            'binding': {
                'session_id': self._active_agent_id or '',
                'agent_id': getattr(self, '_bound_agent_id', None) or '',
                'mode': 'bound-agent' if getattr(self, '_bound_agent_id', None) else 'fresh-session',
            },
            'tokens': {
                'chat_in': 0,
                'chat_out': 0,
                'summary_tokens': 0,
                'goal_inference_tokens': int(getattr(self, '_goal_inference_token_estimate', 0) or 0),
                'consolidation_tokens': 0,
                'max_context': 0,
            },
        }

        # Session-local outcome ledger
        if hasattr(self, '_session_tasks'):
            info['tasks'] = self._session_tasks[-50:]
            info['tokens']['chat_in'] = sum(t.get('tokens_in', 0) for t in self._session_tasks)
            info['tokens']['chat_out'] = sum(t.get('tokens_out', 0) for t in self._session_tasks)

        try:
            if self.engine and getattr(self.engine, 'model', None):
                info['tokens']['max_context'] = int(getattr(self.engine.model, 'context_window', 0) or 0)
        except Exception as e:
            _diag('dashboard', 'engine context window unavailable; max_context reported as 0', error=e)

        # Goals: session-level + project-level
        try:
            from charon.agents.goal_runtime import list_goals, _safe_id, _read_json, _session_path
            onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
            project = str(onboarding.get('project') or str(common.ROOT)).strip()
            import time as _time
            cutoff = _time.time() - 86400

            # Session goals (current session)
            if self._active_agent_id:
                session_id = _safe_id(self._active_agent_id, 'session')
                ses_doc = _read_json(_session_path(common.STATE_DIR, session_id), {})
                ses_goals = [g for g in (ses_doc.get('goals') or []) if isinstance(g, dict)]
                info['goal_summary']['active_goal_id'] = str(ses_doc.get('active_goal_id') or '')
                info['goal_summary']['session_total'] = len(ses_goals)
                for g in ses_goals[-10:]:
                    info['goals'].append({
                        'id': g.get('goal_id', ''),
                        'title': g.get('title', '')[:80],
                        'status': g.get('status', ''),
                        'intent_type': g.get('intent_type', ''),
                        'criteria': g.get('acceptance_criteria', []),
                        'scope': 'session',
                    })

            # Project goals (active/recent only, skip duplicates from session)
            session_ids = {g['id'] for g in info['goals']}
            all_goals = list_goals(common.STATE_DIR, project=project)
            info['goal_summary']['project_total'] = len(all_goals)
            for g in all_goals:
                status = str(g.get('status') or '')
                if status == 'proposed':
                    info['goal_summary']['proposed'] += 1
                elif status == 'confirmed':
                    info['goal_summary']['confirmed'] += 1
                elif status == 'executing':
                    info['goal_summary']['executing'] += 1
                elif status == 'verifying':
                    info['goal_summary']['verifying'] += 1
                elif status == 'active':
                    info['goal_summary']['active'] += 1
                elif status == 'backlog':
                    info['goal_summary']['backlog'] += 1
                elif status == 'blocked':
                    info['goal_summary']['blocked'] += 1
                elif status == 'completed':
                    info['goal_summary']['completed_recent'] += 1
                elif status == 'failed':
                    info['goal_summary']['failed_recent'] += 1
            stale_cutoff = _time.time() - 7 * 86400  # 7 days
            stale_iso = __import__('datetime').datetime.fromtimestamp(stale_cutoff).isoformat()
            all_goals = [g for g in all_goals if 
                g.get('goal_id', '') not in session_ids and (
                    (g.get('status') in ('active', 'backlog', 'proposed', 'confirmed') 
                     and g.get('created_at', '') > stale_iso) or
                    (g.get('status') == 'completed' and g.get('completed_at', '') > 
                        __import__('datetime').datetime.fromtimestamp(cutoff).isoformat())
                )]
            for g in all_goals[-10:]:
                info['goals'].append({
                    'id': g.get('goal_id', ''),
                    'title': g.get('title', '')[:80],
                    'status': g.get('status', ''),
                    'intent_type': g.get('intent_type', ''),
                    'criteria': g.get('acceptance_criteria', []),
                    'scope': 'project',
                })
        except Exception as e:
            _diag('dashboard', 'goal summary computation failed; session goals omitted', error=e)

        # User model (rendered)
        try:
            from charon.memory.user_model_structured import load_structured, render_for_prompt
            model = load_structured(common.STATE_DIR)
            info['user_model'] = render_for_prompt(model)
        except Exception as e:
            _diag('dashboard', 'user model rendering failed; user model pane empty', error=e)

        # Active transfer metadata, if current engine was resumed via transfer
        try:
            if self.engine and getattr(self.engine, 'transfer_bundle', None):
                bundle = self.engine.transfer_bundle
                compiled = getattr(self.engine, 'transfer_compiled', {}) or {}
                info['transfer'] = {
                    'id': bundle.get('id', ''),
                    'source_provider': bundle.get('source', {}).get('provider', ''),
                    'target_provider': bundle.get('target', {}).get('provider', ''),
                    'full_message_count': bundle.get('history', {}).get('full_message_count', 0),
                    'full_transcript_path': bundle.get('history', {}).get('full_transcript_path', ''),
                    'profile_name': compiled.get('profile_name', ''),
                    'tier': compiled.get('tier', ''),
                    'budget_tokens': compiled.get('budget_tokens', 0),
                    'applied_tokens_estimate': compiled.get('applied_tokens_estimate', 0),
                    'replayed_messages': compiled.get('replayed_messages', 0),
                    'tool_history_mode': compiled.get('strategy', {}).get('tool_history_mode', ''),
                    'message_mode': compiled.get('strategy', {}).get('message_mode', ''),
                    'omitted': compiled.get('omitted', {}),
                }
        except Exception as e:
            _diag('dashboard', 'transfer metadata read failed; transfer info omitted', error=e)

        # Token usage from consolidation traces
        try:
            from charon.memory.consolidation import list_traces
            traces = list_traces(common.STATE_DIR, limit=10)
            # Rough estimate: each consolidation uses ~1K tokens
            info['tokens']['consolidation_tokens'] = len(traces) * 1000
        except Exception as e:
            _diag('dashboard', 'consolidation trace count unavailable; consolidation tokens zeroed', error=e)

        return info

    def _get_batch_progress(self) -> str:
        """Short progress string for active batches."""
        try:
            from charon.automation.batch_orchestrator import list_batches
            running = [b for b in list_batches(common.STATE_DIR) if b.get('status') == 'running']
            if not running:
                return ''
            total_done = sum(b.get('completed_count', 0) for b in running)
            total_all = sum(b.get('total', 0) for b in running)
            total_failed = sum(b.get('failed_count', 0) for b in running)
            parts = [f'({total_done}/{total_all})']
            if total_failed:
                parts.append(f'{total_failed}✗')
            if len(running) > 1:
                parts.append(f'{len(running)} batches')
            return ' '.join(parts)
        except Exception as e:
            _diag('dashboard', 'batch listing failed; batch progress hidden', error=e)
            return ''
