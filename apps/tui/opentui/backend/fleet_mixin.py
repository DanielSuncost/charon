"""/fleet setup flow mixin."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend import common


class FleetSetupMixin:
    """Interactive /fleet setup flow."""

    def _start_fleet_setup(self, host: str, user: str, request_id: str | None):
        """Begin the interactive fleet setup flow."""
        target = f'{user}@{host}' if user else host

        self._pending_fleet_setup = {
            'step': 'ssh_test',
            'host': host,
            'user': user,
            'target': target,
            'server_id': host.split('.')[0] if '.' in host else host,
            'agents': [],
            'request_id': request_id,
        }

        common.emit({'type': 'status', 'message': f'═══ Fleet Setup: {target} ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})

        # Step 1: Test SSH (non-interactive, runs immediately)
        import threading
        def _test_and_continue():
            try:
                result = subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', target, 'echo ok'],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode != 0:
                    common.emit({'type': 'error', 'error': f'SSH connection failed: {result.stderr.strip()[:200]}', 'request_id': request_id})
                    self._pending_fleet_setup = None
                    return

                common.emit({'type': 'status', 'message': '✓ SSH connected', 'request_id': request_id})

                # Check if charon is installed
                check = subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', target, 'test -d ~/charon && echo installed || echo missing'],
                    capture_output=True, text=True, timeout=15,
                )
                charon_installed = 'installed' in check.stdout

                # Check if charons-boat is available
                boat_check = subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', target, 'export PATH="$HOME/.local/bin:$PATH" && charons-boat version 2>/dev/null || echo missing'],
                    capture_output=True, text=True, timeout=15,
                )
                boat_installed = 'missing' not in boat_check.stdout

                self._pending_fleet_setup['charon_installed'] = charon_installed
                self._pending_fleet_setup['boat_installed'] = boat_installed

                if not charon_installed:
                    common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': 'Charon is not installed on this server.', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': '  1. Install full Charon (agents with tools, memory, provider)', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': '  2. Skip (use Harbor bash dispatch only)', 'request_id': request_id})
                    self._pending_fleet_setup['step'] = 'confirm_install'
                else:
                    common.emit({'type': 'status', 'message': '✓ Charon already installed', 'request_id': request_id})
                    if not boat_installed:
                        common.emit({'type': 'status', 'message': 'Deploying charons-boat...', 'request_id': request_id})
                        self._fleet_setup_deploy_boat(request_id)
                    else:
                        common.emit({'type': 'status', 'message': '✓ charons-boat available', 'request_id': request_id})
                    self._fleet_setup_ask_agents(request_id)
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Fleet setup failed: {e}', 'request_id': request_id})
                self._pending_fleet_setup = None

        threading.Thread(target=_test_and_continue, daemon=True).start()

    def _handle_fleet_setup_response(self, response: str, request_id: str | None):
        """Handle user responses during fleet setup flow."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        step = setup.get('step', '')
        raw_response = response.strip()
        response = raw_response.lower()

        if step == 'enter_host':
            target = raw_response
            if not target:
                common.emit({'type': 'error', 'error': 'Enter a server address like deploy@65.21.191.198', 'request_id': request_id})
                return
            if '@' in target:
                user, host = target.rsplit('@', 1)
            else:
                user, host = '', target
            self._pending_fleet_setup = None
            self._start_fleet_setup(host, user, request_id)
            return

        if step == 'confirm_install':
            if response in ('1', 'yes', 'y'):
                common.emit({'type': 'status', 'message': 'Installing Charon on remote server...', 'request_id': request_id})
                common.emit({'type': 'status', 'message': '(cloning repo + installing deps — may take a few minutes)', 'request_id': request_id})
                import threading
                threading.Thread(target=self._fleet_setup_install_remote, args=(request_id,), daemon=True).start()
            elif response in ('2', 'no', 'n', 'skip'):
                common.emit({'type': 'status', 'message': 'Skipping install. Deploying charons-boat for Harbor dispatch.', 'request_id': request_id})
                self._fleet_setup_deploy_boat(request_id)
                self._fleet_setup_ask_agents(request_id)
            else:
                common.emit({'type': 'status', 'message': 'Pick 1 (install) or 2 (skip)', 'request_id': request_id})

        elif step == 'choose_agents':
            # Parse agent selection: "ops,builder" or "1,2" or custom names
            preset_agents = {
                '1': {'name': 'ops', 'specialization': 'deployment, releases, docker, server management'},
                '2': {'name': 'builder', 'specialization': 'feature implementation, code changes, testing'},
                '3': {'name': 'watchdog', 'specialization': 'health monitoring, log analysis, alerting'},
            }
            agents = []
            for part in response.replace(' ', ',').split(','):
                part = part.strip()
                if part in preset_agents:
                    agents.append(preset_agents[part])
                elif part in ('ops', 'builder', 'watchdog'):
                    matching = [a for a in preset_agents.values() if a['name'] == part]
                    if matching:
                        agents.append(matching[0])
                elif part:
                    agents.append({'name': part, 'specialization': ''})

            if not agents:
                common.emit({'type': 'error', 'error': 'No agents selected. Pick from: 1 (ops), 2 (builder), 3 (watchdog), or custom names', 'request_id': request_id})
                return

            setup['agents'] = agents
            agent_names = ', '.join(a['name'] for a in agents)
            common.emit({'type': 'status', 'message': f'Agents: {agent_names}', 'request_id': request_id})
            self._fleet_setup_ask_auth(request_id)

        elif step == 'choose_auth':
            if response in ('1', 'copy'):
                common.emit({'type': 'status', 'message': 'Copying local credentials to remote...', 'request_id': request_id})
                import threading
                threading.Thread(target=self._fleet_setup_copy_auth, args=(request_id,), daemon=True).start()
            elif response in ('2', 'oauth'):
                common.emit({'type': 'status', 'message': 'Starting OAuth flow for remote...', 'request_id': request_id})
                import threading
                threading.Thread(target=self._fleet_setup_remote_oauth, args=(request_id,), daemon=True).start()
            elif response in ('3', 'key', 'api'):
                common.emit({'type': 'status', 'message': 'Paste your API key:', 'request_id': request_id})
                setup['step'] = 'paste_api_key'
            else:
                common.emit({'type': 'error', 'error': 'Pick: 1 (copy local), 2 (OAuth), or 3 (API key)', 'request_id': request_id})

        elif step == 'paste_api_key':
            api_key = response.strip()
            if not api_key or len(api_key) < 10:
                common.emit({'type': 'error', 'error': 'That doesn\'t look like a valid API key. Try again:', 'request_id': request_id})
                return
            import threading
            threading.Thread(target=self._fleet_setup_write_api_key, args=(api_key, request_id), daemon=True).start()

        elif step == 'paste_oauth_code':
            code = response.strip()
            if not code:
                common.emit({'type': 'error', 'error': 'Paste the authorization code from the browser:', 'request_id': request_id})
                return
            import threading
            threading.Thread(target=self._fleet_setup_exchange_oauth_code, args=(code, request_id), daemon=True).start()

        else:
            common.emit({'type': 'error', 'error': f'Unexpected fleet setup state: {step}', 'request_id': request_id})
            self._pending_fleet_setup = None

    def _fleet_setup_deploy_boat(self, request_id: str | None):
        """Deploy charons-boat to the remote server."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        if setup.get('boat_installed'):
            return
        target = setup['target']
        try:
            script_dir = Path(__file__).resolve().parents[2] / 'tools' / 'charons-boat'
            result = subprocess.run(
                [str(script_dir / 'charons-boat'), 'deploy', target],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                common.emit({'type': 'status', 'message': '✓ charons-boat deployed', 'request_id': request_id})
                setup['boat_installed'] = True
            else:
                common.emit({'type': 'status', 'message': f'Boat deploy issue: {result.stderr.strip()[:200]}', 'request_id': request_id})
        except Exception as e:
            common.emit({'type': 'status', 'message': f'Boat deploy error: {e}', 'request_id': request_id})

    def _fleet_setup_install_remote(self, request_id: str | None):
        """Install full Charon on remote server via SSH."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        target = setup['target']
        try:
            # Step 1: Clone repo
            common.emit({'type': 'status', 'message': '  Cloning charon repo...', 'request_id': request_id})
            clone_result = subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', target,
                 'test -d ~/charon && echo exists || git clone --depth=1 https://github.com/DanielSuncost/charon.git ~/charon'],
                capture_output=True, text=True, timeout=120,
            )
            if clone_result.returncode != 0:
                common.emit({'type': 'error', 'error': f'Clone failed: {clone_result.stderr.strip()[:200]}', 'request_id': request_id})
                common.emit({'type': 'status', 'message': 'Falling back to charons-boat only.', 'request_id': request_id})
                self._fleet_setup_deploy_boat(request_id)
                self._fleet_setup_ask_agents(request_id)
                return
            common.emit({'type': 'status', 'message': '  ✓ Repo ready', 'request_id': request_id})

            # Step 2: Run install script
            common.emit({'type': 'status', 'message': '  Running install.sh (deps + build)...', 'request_id': request_id})
            install_result = subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=30', target,
                 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && '
                 'cd ~/charon && ./scripts/install.sh --no-playwright --no-tmux -y'],
                capture_output=True, text=True, timeout=600,  # 10 minutes
            )
            if install_result.returncode == 0:
                common.emit({'type': 'status', 'message': '  ✓ Charon installed', 'request_id': request_id})
                setup['charon_installed'] = True
                setup['boat_installed'] = True
            else:
                # Show last few lines of output for debugging
                output = (install_result.stdout + install_result.stderr).strip()
                last_lines = '\n'.join(output.splitlines()[-5:])
                common.emit({'type': 'error', 'error': f'Install failed:\n{last_lines}', 'request_id': request_id})
                common.emit({'type': 'status', 'message': 'Falling back to charons-boat only.', 'request_id': request_id})
                self._fleet_setup_deploy_boat(request_id)

            self._fleet_setup_ask_agents(request_id)
        except subprocess.TimeoutExpired:
            common.emit({'type': 'error', 'error': 'Install timed out (10 min). Try running install.sh manually on the server.', 'request_id': request_id})
            common.emit({'type': 'status', 'message': 'Continuing with charons-boat only.', 'request_id': request_id})
            self._fleet_setup_deploy_boat(request_id)
            self._fleet_setup_ask_agents(request_id)
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Remote install failed: {e}', 'request_id': request_id})
            self._fleet_setup_deploy_boat(request_id)
            self._fleet_setup_ask_agents(request_id)

    def _fleet_setup_ask_agents(self, request_id: str | None):
        """Prompt user to choose which agents to set up."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Which agents do you want on this server?', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  1. ops — deployment, releases, server management', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  2. builder — feature implementation, code changes', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  3. watchdog — health monitoring, alerting', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  Or type custom names (comma-separated)', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Example: 1,2 or ops,builder,custom-name', 'request_id': request_id})
        setup['step'] = 'choose_agents'

    def _fleet_setup_ask_auth(self, request_id: str | None):
        """Prompt user for provider auth method."""
        setup = self._pending_fleet_setup
        if not setup:
            return

        # Detect local provider
        local_onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        local_provider = local_onboarding.get('provider', '')
        local_model = local_onboarding.get('model', '')

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Provider setup for remote agents:', 'request_id': request_id})
        if local_provider:
            common.emit({'type': 'status', 'message': f'  Your local Charon uses: {local_provider} ({local_model})', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  1. Copy local credentials to remote (recommended)', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  2. Fresh OAuth login (opens browser link to paste code)', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  3. Paste an API key directly', 'request_id': request_id})
        setup['step'] = 'choose_auth'

    def _fleet_setup_copy_auth(self, request_id: str | None):
        """Copy local auth + onboarding config to remote."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        target = setup['target']
        try:
            # Copy auth.json
            auth_file = common.STATE_DIR / 'auth' / 'auth.json'
            if auth_file.exists():
                subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', target, 'mkdir -p ~/charon/.charon_state/auth'],
                    capture_output=True, timeout=10,
                )
                subprocess.run(
                    ['scp', str(auth_file), f'{target}:~/charon/.charon_state/auth/auth.json'],
                    capture_output=True, timeout=15,
                )

            # Copy onboarding.json
            onboarding_file = common.STATE_DIR / 'onboarding.json'
            if onboarding_file.exists():
                subprocess.run(
                    ['scp', str(onboarding_file), f'{target}:~/charon/.charon_state/onboarding.json'],
                    capture_output=True, timeout=15,
                )

            common.emit({'type': 'status', 'message': '✓ Credentials copied to remote', 'request_id': request_id})
            self._fleet_setup_start_agents(request_id)
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Failed to copy credentials: {e}', 'request_id': request_id})
            self._pending_fleet_setup = None

    def _fleet_setup_remote_oauth(self, request_id: str | None):
        """Start OAuth flow on remote, present URL to user for code paste."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        target = setup['target']
        try:
            # Run charon auth on remote — it will output AUTH_URL::
            result = subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', target,
                 'export PATH="$HOME/.local/bin:$HOME/charon:$PATH" && '
                 'cd ~/charon && PYTHONPATH=src python3 -c "'
                 'from charon.providers import charon_auth; '
                 'charon_auth.login_oauth(\\"openai-codex\\", status_cb=print)'
                 '"'],
                capture_output=True, text=True, timeout=60,
            )

            # Extract AUTH_URL from output
            auth_url = ''
            for line in result.stdout.splitlines():
                if 'AUTH_URL::' in line:
                    auth_url = line.split('AUTH_URL::', 1)[1].strip()
                    break

            if auth_url:
                common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                common.emit({'type': 'status', 'message': 'Open this URL in your browser:', 'request_id': request_id})
                common.emit({'type': 'status', 'message': f'  {auth_url}', 'request_id': request_id})
                common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                common.emit({'type': 'status', 'message': 'After signing in, paste the authorization code:', 'request_id': request_id})
                setup['step'] = 'paste_oauth_code'
                setup['auth_url'] = auth_url
            else:
                # Maybe auth succeeded directly (tokens existed)
                if 'Tokens stored' in result.stdout:
                    common.emit({'type': 'status', 'message': '✓ Remote authenticated', 'request_id': request_id})
                    self._fleet_setup_start_agents(request_id)
                else:
                    common.emit({'type': 'error', 'error': f'OAuth setup failed: {result.stderr.strip()[:200]}', 'request_id': request_id})
                    self._pending_fleet_setup = None
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Remote OAuth failed: {e}', 'request_id': request_id})
            self._pending_fleet_setup = None

    def _fleet_setup_exchange_oauth_code(self, code: str, request_id: str | None):
        """Exchange OAuth code on remote server."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        target = setup['target']
        try:
            result = subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', target,
                 f'export PATH="$HOME/.local/bin:$HOME/charon:$PATH" && '
                 f'cd ~/charon && PYTHONPATH=src python3 -c "'
                 f'from charon.providers import charon_auth; '
                 f'charon_auth.login_oauth(\\"openai-codex\\", auth_code_cb=lambda p: \\"{code}\\")'
                 f'"'],
                capture_output=True, text=True, timeout=30,
            )
            if 'Tokens stored' in result.stdout or result.returncode == 0:
                common.emit({'type': 'status', 'message': '✓ Remote authenticated', 'request_id': request_id})
                self._fleet_setup_start_agents(request_id)
            else:
                common.emit({'type': 'error', 'error': f'Token exchange failed: {result.stderr.strip()[:200]}', 'request_id': request_id})
                self._pending_fleet_setup = None
        except Exception as e:
            common.emit({'type': 'error', 'error': f'OAuth exchange failed: {e}', 'request_id': request_id})
            self._pending_fleet_setup = None

    def _fleet_setup_write_api_key(self, api_key: str, request_id: str | None):
        """Write API key to remote server."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        target = setup['target']
        try:
            # Write to remote onboarding
            onboarding = {
                'provider': 'api',
                'provider_auth': 'api-key',
                'model': '',
                'complete': True,
                'step': 'done',
            }
            auth = {'providers': {'api': {'tokens': {'access_token': api_key}, 'auth_type': 'api-key'}}}

            subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', target,
                 f'mkdir -p ~/charon/.charon_state/auth && '
                 f'echo \'{json.dumps(onboarding)}\' > ~/charon/.charon_state/onboarding.json && '
                 f'echo \'{json.dumps(auth)}\' > ~/charon/.charon_state/auth/auth.json'],
                capture_output=True, timeout=15,
            )
            common.emit({'type': 'status', 'message': '✓ API key configured on remote', 'request_id': request_id})
            self._fleet_setup_start_agents(request_id)
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Failed to write API key: {e}', 'request_id': request_id})
            self._pending_fleet_setup = None

    def _fleet_setup_start_agents(self, request_id: str | None):
        """Start the configured agents on the remote server and update fleet.json."""
        setup = self._pending_fleet_setup
        if not setup:
            return
        target = setup['target']
        agents = setup.get('agents', [])
        charon_installed = setup.get('charon_installed', False)

        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Starting agents...', 'request_id': request_id})

        for agent in agents:
            name = agent['name']
            if charon_installed:
                cmd = f'export PATH="$HOME/.local/bin:$HOME/charon:$PATH" && charons-boat wrap --name {name} -- ~/charon/charon chat'
            else:
                cmd = f'export PATH="$HOME/.local/bin:$PATH" && charons-boat wrap --name {name} -- bash'
            try:
                result = subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', target, cmd],
                    capture_output=True, text=True, timeout=30,
                )
                agent_type = 'charon' if charon_installed else 'bash'
                if result.returncode == 0:
                    common.emit({'type': 'status', 'message': f'  ✓ {name} started ({agent_type})', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': f'  ✗ {name} failed: {result.stderr.strip()[:100]}', 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'status', 'message': f'  ✗ {name} failed: {e}', 'request_id': request_id})

        # Update fleet.json
        try:
            from charon.fleet.fleet_registry import load_fleet, save_fleet
            fleet = load_fleet()
            server_id = setup.get('server_id', setup['host'])

            # Remove existing entry for this server if any
            fleet['servers'] = [s for s in fleet.get('servers', []) if s.get('id') != server_id]

            fleet['servers'].append({
                'id': server_id,
                'host': setup['host'],
                'user': setup['user'],
                'agents': [
                    {
                        'name': a['name'],
                        'type': 'charon' if charon_installed else 'bash',
                        'specialization': a.get('specialization', ''),
                        'project': '',
                        'auto_start': True,
                    }
                    for a in agents
                ],
            })
            save_fleet(fleet)
            common.emit({'type': 'status', 'message': '', 'request_id': request_id})
            common.emit({'type': 'status', 'message': '✓ Fleet config updated', 'request_id': request_id})
        except Exception as e:
            common.emit({'type': 'status', 'message': f'Fleet config warning: {e}', 'request_id': request_id})

        # Done
        agent_names = ', '.join(a['name'] for a in agents)
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '═══ Setup complete ═══', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Server: {target}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Agents: {agent_names}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Type: {"full Charon agents" if charon_installed else "bash (Harbor dispatch)"}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
        common.emit({'type': 'status', 'message': 'Try:', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'  /voyage dispatch {server_id} {agents[0]["name"]} "hostname && uptime"', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  /fleet status', 'request_id': request_id})
        common.emit({'type': 'status', 'message': '  F3 to see agents in Session Grid', 'request_id': request_id})

        self._pending_fleet_setup = None
