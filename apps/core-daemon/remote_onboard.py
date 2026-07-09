"""Remote onboarding — test SSH, discover agents, deploy boat, auto-configure fleet."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from fleet_registry import load_fleet, save_fleet

SSH_TIMEOUT = 10

# Known agent processes to detect on remote servers
KNOWN_AGENTS = {
    'claude': 'claude-code',
    'pi': 'pi',
    'codex': 'codex',
    'hermes': 'hermes',
    'opencode': 'opencode',
    'aider': 'aider',
    'cursor': 'cursor',
}


def _ssh_target(host: str, user: str = '') -> str:
    return f'{user}@{host}' if user else host


def _run_ssh(host: str, user: str, *remote_cmd: str, timeout: int = SSH_TIMEOUT + 5) -> subprocess.CompletedProcess:
    """Run a command on remote via SSH."""
    target = _ssh_target(host, user)
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={SSH_TIMEOUT}', target] + list(remote_cmd)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def test_ssh(host: str, user: str = '') -> tuple[bool, str]:
    """Test SSH connectivity. Returns (success, message)."""
    target = _ssh_target(host, user)
    try:
        result = _run_ssh(host, user, 'echo', 'charon-ssh-ok')
        if result.returncode == 0 and 'charon-ssh-ok' in result.stdout:
            return True, f'SSH connection to {target} successful'
        stderr = result.stderr.strip()
        if 'Permission denied' in stderr:
            return False, f'SSH auth failed for {target}. Set up key-based auth:\n  ssh-copy-id {target}'
        if 'Connection refused' in stderr:
            return False, f'SSH connection refused on {host}. Check that sshd is running.'
        if 'timed out' in stderr.lower() or 'No route' in stderr:
            return False, f'Cannot reach {host}. Check the IP/hostname and network.'
        return False, f'SSH failed for {target}: {stderr or "unknown error"}'
    except subprocess.TimeoutExpired:
        return False, f'SSH to {target} timed out after {SSH_TIMEOUT}s. Check host is reachable.'
    except Exception as e:
        return False, f'SSH error: {e}'


def check_boat_installed(host: str, user: str = '') -> tuple[bool, str]:
    """Check if charons-boat is available on the remote."""
    try:
        result = _run_ssh(host, user, 'which', 'charons-boat')
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip()
        # Also check ~/.local/bin
        result2 = _run_ssh(host, user, 'test', '-x', '$HOME/.local/bin/charons-boat', '&&', 'echo', 'found')
        if result2.returncode == 0 and 'found' in result2.stdout:
            return True, '~/.local/bin/charons-boat'
        return False, 'charons-boat not found on remote'
    except Exception as e:
        return False, f'Check failed: {e}'


def deploy_boat_remote(host: str, user: str = '') -> tuple[bool, str]:
    """Deploy charons-boat to remote server using the boat deploy command."""
    target = _ssh_target(host, user)
    script_dir = Path(__file__).resolve().parents[2] / 'tools' / 'charons-boat'
    boat_script = script_dir / 'charons-boat'
    if not boat_script.exists():
        return False, f'Local charons-boat not found at {boat_script}'
    try:
        result = subprocess.run(
            [str(boat_script), 'deploy', target],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, f'Deploy failed: {result.stderr.strip() or result.stdout.strip()}'
    except Exception as e:
        return False, f'Deploy error: {e}'


def discover_remote_agents(host: str, user: str = '') -> dict:
    """Discover all agents on remote: boat-wrapped + tmux sessions.

    Returns:
        {
            'boat_sessions': [...],  # Already boat-wrapped
            'tmux_agents': [...],    # Tmux sessions with recognized agents
            'tmux_other': [...],     # Other tmux sessions
            'error': str | None,
        }
    """
    result = {
        'boat_sessions': [],
        'tmux_agents': [],
        'tmux_other': [],
        'error': None,
    }

    # 1. Try charons-boat stream to get boat-wrapped sessions
    boat_session_names: set[str] = set()
    try:
        target = _ssh_target(host, user)
        proc = subprocess.Popen(
            ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={SSH_TIMEOUT}',
             target, 'charons-boat', 'stream'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + SSH_TIMEOUT + 5
        while time.monotonic() < deadline and proc.stdout:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get('type') == 'sessions':
                for sess in msg.get('sessions', []):
                    name = sess.get('name', sess.get('id', ''))
                    agent = sess.get('agent', '')
                    transport = sess.get('transport', 'pty')
                    boat_session_names.add(name)
                    if transport == 'tmux':
                        # Boat already discovered this as a tmux agent
                        result['tmux_agents'].append({
                            'name': name,
                            'type': agent,
                            'status': sess.get('status', 'running'),
                            'source': 'boat-tmux',
                            'tmux_session': sess.get('tmux_session', name),
                        })
                    else:
                        result['boat_sessions'].append({
                            'name': name,
                            'type': agent,
                            'status': sess.get('status', 'running'),
                            'source': 'boat',
                        })
                break
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass  # Boat may not be installed yet

    # 2. Discover tmux sessions directly via SSH
    try:
        tmux_result = _run_ssh(
            host, user,
            'tmux', 'list-panes', '-a', '-F',
            '#{session_name}\t#{pane_pid}\t#{pane_current_command}',
            timeout=15,
        )
        if tmux_result.returncode == 0:
            seen: set[str] = set()
            for line in tmux_result.stdout.strip().splitlines():
                parts = line.split('\t')
                if len(parts) < 3:
                    continue
                sess_name, pane_pid, cmd = parts[0], parts[1], parts[2]
                if sess_name in seen or sess_name in boat_session_names:
                    continue
                if sess_name.startswith('boat-'):
                    continue
                seen.add(sess_name)

                # Detect agent type from command name
                agent_type = None
                cmd_lower = cmd.lower()
                for key, atype in KNOWN_AGENTS.items():
                    if key in cmd_lower:
                        agent_type = atype
                        break

                # If not found in command, check child processes
                if not agent_type:
                    try:
                        child_result = _run_ssh(
                            host, user,
                            'pgrep', '-a', '-P', pane_pid,
                            timeout=10,
                        )
                        if child_result.returncode == 0:
                            for cline in child_result.stdout.strip().splitlines():
                                cline_lower = cline.lower()
                                for key, atype in KNOWN_AGENTS.items():
                                    if key in cline_lower:
                                        agent_type = atype
                                        break
                                if agent_type:
                                    break
                    except Exception:
                        pass

                entry = {
                    'name': sess_name,
                    'type': agent_type or 'unknown',
                    'status': 'running',
                    'source': 'tmux',
                    'tmux_session': sess_name,
                }
                if agent_type:
                    # Don't duplicate if boat already found this
                    if not any(a['name'] == sess_name for a in result['tmux_agents']):
                        result['tmux_agents'].append(entry)
                else:
                    result['tmux_other'].append(entry)
    except Exception as e:
        result['error'] = f'tmux discovery failed: {e}'

    return result


def _generate_server_id(host: str) -> str:
    """Generate a clean server ID from hostname/IP."""
    clean = re.sub(r'[^a-z0-9]+', '-', host.lower()).strip('-')
    return f'server-{clean}'[:40] if clean else 'server-unknown'


def auto_configure_fleet(
    host: str,
    user: str,
    agents: list[dict],
    server_id: str = '',
) -> dict:
    """Add server + agents to fleet.json. Returns the new server entry.

    Args:
        host: Remote hostname or IP
        user: SSH username
        agents: List of dicts with 'name', 'type', 'specialization', 'project'
        server_id: Optional custom server ID (auto-generated from host if empty)
    """
    fleet = load_fleet()

    if not server_id:
        server_id = _generate_server_id(host)

    # Check for existing server with same ID or host
    existing = next(
        (s for s in fleet.get('servers', [])
         if s.get('id') == server_id or s.get('host') == host),
        None,
    )
    if existing:
        # Merge agents into existing server
        existing_names = {a.get('name') for a in existing.get('agents', [])}
        for agent in agents:
            if agent['name'] not in existing_names:
                existing.setdefault('agents', []).append({
                    'name': agent['name'],
                    'type': agent.get('type', 'unknown'),
                    'specialization': agent.get('specialization', ''),
                    'project': agent.get('project', ''),
                    'auto_start': False,
                })
        save_fleet(fleet)
        return existing

    # Create new server entry
    server = {
        'id': server_id,
        'host': host,
        'user': user,
        'ssh_options': [
            '-o', 'ControlMaster=auto',
            '-o', 'ControlPath=~/.ssh/charon-%r@%h:%p',
            '-o', 'ControlPersist=600',
        ],
        'boat_command': 'charons-boat stream',
        'agents': [
            {
                'name': a['name'],
                'type': a.get('type', 'unknown'),
                'specialization': a.get('specialization', ''),
                'project': a.get('project', ''),
                'auto_start': False,
            }
            for a in agents
        ],
    }
    fleet.setdefault('servers', []).append(server)
    save_fleet(fleet)
    return server


def full_onboard(host: str, user: str = '') -> list[dict]:
    """Run the complete onboarding flow. Returns list of status messages.

    Each message is {'step': str, 'ok': bool, 'message': str, 'data': Any}.
    """
    messages: list[dict] = []

    # Step 1: Test SSH
    ok, msg = test_ssh(host, user)
    messages.append({'step': 'ssh', 'ok': ok, 'message': msg})
    if not ok:
        return messages

    # Step 2: Check boat
    boat_ok, boat_msg = check_boat_installed(host, user)
    messages.append({'step': 'boat_check', 'ok': boat_ok, 'message': boat_msg})

    # Step 3: Deploy boat if needed
    if not boat_ok:
        dep_ok, dep_msg = deploy_boat_remote(host, user)
        messages.append({'step': 'boat_deploy', 'ok': dep_ok, 'message': dep_msg})
        if not dep_ok:
            messages.append({'step': 'boat_deploy_hint', 'ok': False,
                           'message': f'Manual install: scp tools/charons-boat/* {_ssh_target(host, user)}:~/.local/bin/'})
            # Continue anyway — tmux discovery still works without boat

    # Step 4: Discover agents
    discovery = discover_remote_agents(host, user)
    all_agents = discovery['boat_sessions'] + discovery['tmux_agents']
    messages.append({
        'step': 'discover',
        'ok': True,
        'message': f"Found {len(all_agents)} agent(s): "
                   f"{len(discovery['boat_sessions'])} boat-wrapped, "
                   f"{len(discovery['tmux_agents'])} in tmux"
                   + (f", {len(discovery['tmux_other'])} other tmux sessions" if discovery['tmux_other'] else ''),
        'data': discovery,
    })

    return messages
