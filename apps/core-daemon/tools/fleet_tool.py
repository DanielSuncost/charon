"""Fleet tools — remote agent orchestration via Charon's boat protocol."""
from __future__ import annotations

from typing import Any

FLEET_STATUS_TOOL_DEF = {
    'name': 'FleetStatus',
    'description': 'Get the current status of all remote servers and agents in the fleet. Returns each server\'s connection status and the status of each agent on it (running, idle, offline).',
    'input_schema': {
        'type': 'object',
        'properties': {
            'server_id': {
                'type': 'string',
                'description': 'Optional: filter to a specific server by its ID. If omitted, returns all servers.',
            },
        },
        'required': [],
    },
}

FLEET_SEND_TOOL_DEF = {
    'name': 'FleetSend',
    'description': 'Send a message or command to a remote agent. The text is typed into the agent\'s terminal as if the user were typing it.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'server_id': {
                'type': 'string',
                'description': 'The fleet server ID where the agent runs.',
            },
            'agent_name': {
                'type': 'string',
                'description': 'The name of the agent to send to.',
            },
            'message': {
                'type': 'string',
                'description': 'The message/command to send to the agent.',
            },
        },
        'required': ['server_id', 'agent_name', 'message'],
    },
}

FLEET_HISTORY_TOOL_DEF = {
    'name': 'FleetHistory',
    'description': 'Get recent terminal output from a remote agent. Connects to the agent\'s boat session and captures its output buffer.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'server_id': {
                'type': 'string',
                'description': 'The fleet server ID where the agent runs.',
            },
            'agent_name': {
                'type': 'string',
                'description': 'The name of the agent whose output to retrieve.',
            },
            'timeout': {
                'type': 'number',
                'description': 'How long to wait for output (seconds, default 5).',
            },
        },
        'required': ['server_id', 'agent_name'],
    },
}

FLEET_ONBOARD_TOOL_DEF = {
    'name': 'FleetOnboard',
    'description': 'Add a new remote server to the fleet. Tests SSH connectivity, discovers running agents (both boat-wrapped and tmux sessions), deploys charons-boat if needed, and configures fleet.json automatically.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'host': {
                'type': 'string',
                'description': 'IP address or hostname of the remote server.',
            },
            'user': {
                'type': 'string',
                'description': 'SSH username for the remote server. If omitted, uses current user.',
            },
        },
        'required': ['host'],
    },
}

ALL_FLEET_TOOL_DEFS = [FLEET_STATUS_TOOL_DEF, FLEET_SEND_TOOL_DEF, FLEET_HISTORY_TOOL_DEF, FLEET_ONBOARD_TOOL_DEF]


def execute_fleet_status(params: dict, context: Any) -> Any:
    from tools import ToolResult
    try:
        from fleet_registry import load_fleet
        from fleet_sync import get_cached_fleet_status
    except ImportError:
        return ToolResult(content='Fleet registry not available.', is_error=True)

    fleet = load_fleet()
    status = get_cached_fleet_status()
    filter_server = params.get('server_id', '').strip()

    result_lines = []
    for server in fleet.get('servers', []):
        sid = server.get('id', server.get('host', ''))
        if filter_server and sid != filter_server:
            continue
        server_status = status.get(sid, {})
        online = server_status.get('online', False)
        result_lines.append(f"Server: {sid} ({server.get('host', '')}) — {'online' if online else 'offline'}")

        sessions = server_status.get('sessions', {})
        for agent_cfg in server.get('agents', []):
            aname = agent_cfg.get('name', '')
            sess = sessions.get(aname, {})
            astatus = sess.get('status', 'offline') if online else 'offline'
            spec = agent_cfg.get('specialization', '')
            project = agent_cfg.get('project', '')
            parts = [f"  - {aname}: {astatus}"]
            if spec:
                parts.append(f"specialization={spec}")
            if project:
                parts.append(f"project={project}")
            result_lines.append(', '.join(parts))

    if not result_lines:
        return ToolResult(content='No fleet servers configured. Add servers to ~/.charon/fleet.json.')

    return ToolResult(content='\n'.join(result_lines))


def execute_fleet_send(params: dict, context: Any) -> Any:
    from tools import ToolResult
    try:
        from fleet_sync import send_to_remote_agent
    except ImportError:
        return ToolResult(content='Fleet sync not available.', is_error=True)

    server_id = params.get('server_id', '').strip()
    agent_name = params.get('agent_name', '').strip()
    message = params.get('message', '').strip()

    if not server_id or not agent_name or not message:
        return ToolResult(content='server_id, agent_name, and message are required.', is_error=True)

    # Append newline so it's submitted as a command
    if not message.endswith('\n'):
        message += '\n'

    ok = send_to_remote_agent(server_id, agent_name, message)
    if ok:
        return ToolResult(content=f'Message sent to {agent_name} @ {server_id}.')
    else:
        return ToolResult(content=f'Failed to send message to {agent_name} @ {server_id}. Check that the server is online and the agent is running.', is_error=True)


def execute_fleet_history(params: dict, context: Any) -> Any:
    from tools import ToolResult
    try:
        from fleet_sync import get_remote_agent_history
    except ImportError:
        return ToolResult(content='Fleet sync not available.', is_error=True)

    server_id = params.get('server_id', '').strip()
    agent_name = params.get('agent_name', '').strip()
    timeout = float(params.get('timeout', 5.0))

    if not server_id or not agent_name:
        return ToolResult(content='server_id and agent_name are required.', is_error=True)

    output = get_remote_agent_history(server_id, agent_name, timeout=timeout)
    if output:
        # Strip ANSI escape sequences for readability
        import re
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
        # Limit output size
        if len(clean) > 10000:
            clean = clean[-10000:]
        return ToolResult(content=clean)
    else:
        return ToolResult(content=f'No output captured from {agent_name} @ {server_id}. Agent may be idle or offline.')


def execute_fleet_onboard(params: dict, context: Any) -> Any:
    from tools import ToolResult
    try:
        from remote_onboard import full_onboard, auto_configure_fleet
    except ImportError:
        return ToolResult(content='Remote onboarding module not available.', is_error=True)

    host = params.get('host', '').strip()
    user = params.get('user', '').strip()

    if not host:
        return ToolResult(content='host is required.', is_error=True)

    messages = full_onboard(host, user)
    lines = []
    all_agents = []
    discovery = None

    for msg in messages:
        step = msg.get('step', '')
        ok = msg.get('ok', False)
        text = msg.get('message', '')
        marker = 'OK' if ok else 'FAILED'
        lines.append(f'[{step}] {marker}: {text}')

        if step == 'discover' and msg.get('data'):
            discovery = msg['data']
            all_agents = discovery.get('boat_sessions', []) + discovery.get('tmux_agents', [])

    # Auto-configure if agents were found
    if all_agents:
        server = auto_configure_fleet(host, user, all_agents)
        lines.append(f'\nAdded server "{server["id"]}" with {len(all_agents)} agent(s) to fleet.')
        # Start fleet sync
        try:
            from fleet_sync import start_fleet_sync
            start_fleet_sync()
        except Exception:
            pass
    elif any(m.get('step') == 'ssh' and m.get('ok') for m in messages):
        # SSH works but no agents found — still add the server
        server = auto_configure_fleet(host, user, [])
        lines.append(f'\nAdded server "{server["id"]}" (no agents found yet) to fleet.')

    return ToolResult(content='\n'.join(lines))
