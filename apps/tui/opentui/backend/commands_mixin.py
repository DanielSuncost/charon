"""Slash-command router mixin.

``handle_command`` is a thin dispatcher: it normalizes the input, handles the
two pending interactive flows (provider switch, fleet setup), then routes on
the command's first whitespace-separated token via ``_COMMAND_HANDLERS``.
The per-family ``_cmd_*`` handlers live in ``commands_core``,
``commands_agents``, ``commands_rooms`` and ``commands_work`` (all composed
alongside this mixin on ``ChatBackend``) and preserve the original branch
bodies verbatim.
"""
from __future__ import annotations

from backend import common

#: Sentinel returned by a ``_cmd_*`` handler when the command's first token
#: matched its family but none of the (verbatim-preserved) branch conditions
#: inside matched — e.g. ``/shade stats`` is handled but ``/shade foo`` is
#: not. The router then falls through to the unknown-command suggestions
#: path, exactly as the original if/elif chain did.
UNHANDLED = object()


class CommandsMixin:
    """Slash-command catalog, suggestions, and the /command router."""

    def _command_catalog(self) -> list[dict]:
        """Return available commands with descriptions."""
        return [
            {'cmd': '/help', 'desc': 'Show available commands'},
            {'cmd': '/setup', 'desc': 'Show setup commands'},
            {'cmd': '/setup status', 'desc': 'Show current configuration'},
            {'cmd': '/setup reset', 'desc': 'Reset all configuration'},
            {'cmd': '/setup provider lmstudio', 'desc': 'Use local LM Studio'},
            {'cmd': '/setup provider claude-code', 'desc': 'Use Anthropic Claude (OAuth)'},
            {'cmd': '/setup provider codex', 'desc': 'Use OpenAI Codex (OAuth)'},
            {'cmd': '/setup provider api', 'desc': 'Use custom API endpoint'},
            {'cmd': '/setup model <name>', 'desc': 'Set model name'},
            {'cmd': '/setup api-key <key>', 'desc': 'Set API key directly'},
            {'cmd': '/setup project <path>', 'desc': 'Set project directory'},
            {'cmd': '/setup complete', 'desc': 'Finish setup'},
            {'cmd': '/setup no-provider', 'desc': 'Skip LLM setup (heuristic only)'},
            {'cmd': '/model', 'desc': 'Show current model'},
            {'cmd': '/reset', 'desc': 'Clear conversation'},
            {'cmd': '/dashboard', 'desc': 'Switch to dashboard (F2)'},
            {'cmd': '/sessions', 'desc': 'Switch to sessions (F3)'},
            {'cmd': '/chat', 'desc': 'Switch to chat (F1)'},
            {'cmd': '/hermes', 'desc': 'Launch a wrapped Hermes session in the background'},
            {'cmd': '/pi', 'desc': 'Launch a wrapped pi session in the background'},
            {'cmd': '/conversation hermes teacher student <topic>', 'desc': 'Start a teacher/student Hermes conversation room'},
            {'cmd': '/conversation hermes strategist critic <topic>', 'desc': 'Start a strategist/critic Hermes conversation room'},
            {'cmd': '/conversation hermes planner critic <topic>', 'desc': 'Start a planner/critic Hermes conversation room'},
            {'cmd': '/conversation hermes architect reviewer <topic>', 'desc': 'Start an architect/reviewer Hermes conversation room'},
            {'cmd': '/conversation hermes optimist skeptic <topic>', 'desc': 'Start an optimist/skeptic Hermes conversation room'},
            {'cmd': '/conversation hermes dialogue <topic>', 'desc': 'Start a peer philosophy dialogue between two Hermes agents'},
            {'cmd': '/conversation hermes 2 <topic>', 'desc': 'Start a 2-agent Hermes conversation room'},
            {'cmd': '/team hermes <count> <topic>', 'desc': 'Create a Hermes discussion room/team'},
            {'cmd': '/devteam hermes <count> <goal>', 'desc': 'Create a Hermes developer team room'},
            {'cmd': '/pause-room <room-id>', 'desc': 'Pause a conversation room runner'},
            {'cmd': '/resume-room <room-id>', 'desc': 'Resume a paused conversation room runner'},
            {'cmd': '/say-room <room-id> <message>', 'desc': 'Say something to the whole room so both sides can react'},
            {'cmd': '/inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'desc': 'Inject steering or a message into a room'},
            {'cmd': '/libris <prompt>', 'desc': 'Start a Libris research intake for a broad research prompt'},
            {'cmd': '/libris status <operation_id>', 'desc': 'Inspect Libris swarm state for an operation'},
            {'cmd': '/devop <prompt>', 'desc': 'Start an autonomous software-development operation for a broad build prompt'},
            {'cmd': '/devop status <operation_id>', 'desc': 'Inspect software-dev operation status and workstreams'},
            {'cmd': '/devop stop <operation_id>', 'desc': 'Request stop for a software-dev operation'},
            {'cmd': '/monitor every hour <url>', 'desc': 'Create a recurring website health monitor'},
            {'cmd': '/automate every <n> <unit> check <url>', 'desc': 'Create a recurring automation for HTTP checking'},
            {'cmd': '/automate list', 'desc': 'List all automations'},
            {'cmd': '/automate list cron', 'desc': 'List cron-scheduled automations'},
            {'cmd': '/automate list continuous', 'desc': 'List always-on continuous automations'},
            {'cmd': '/automate list scheduled', 'desc': 'List interval/cron scheduled automations'},
            {'cmd': '/automate status <automation_id>', 'desc': 'Inspect an automation and recent runs'},
            {'cmd': '/automate pause <automation_id>', 'desc': 'Pause a recurring automation'},
            {'cmd': '/automate resume <automation_id>', 'desc': 'Resume a paused recurring automation'},
            {'cmd': '/automate stop <automation_id>', 'desc': 'Stop a recurring automation'},
            {'cmd': '/automate cron "0 9 * * 1-5" check <url>', 'desc': 'Create a cron-scheduled automation'},
            {'cmd': '/automate continuous every <n> seconds check <url>', 'desc': 'Create an always-on loop automation'},
            {'cmd': '/monitor browser every hour <url> expect "text"', 'desc': 'Create a browser-rendered functional monitor'},
            {'cmd': '/automate browser every <n> <unit> check <url> expect "text"', 'desc': 'Create a browser-based rendered-page monitor'},
            {'cmd': '/automate browser-workflow every <n> <unit> steps <json>', 'desc': 'Create a multi-step browser workflow automation'},
            {'cmd': '/automate browser-workflow every <n> <unit> from <file>', 'desc': 'Create a multi-step browser workflow automation from a JSON file'},
            {'cmd': '/automate webhook <automation_id> <url>', 'desc': 'Set a webhook for automation failure/recovery alerts'},
            {'cmd': '/harvest_souls', 'desc': 'Scan sibling agent repos for abilities to assimilate'},
            {'cmd': '/harvest_souls list', 'desc': 'Show numbered findings from last scan'},
            {'cmd': '/harvest_souls evaluate', 'desc': 'Evaluate real capability gaps from the last scan'},
            {'cmd': '/harvest_souls review', 'desc': 'Show capability-level harvest decisions'},
            {'cmd': '/harvest_souls decide', 'desc': 'Inspect one capability harvest decision'},
            {'cmd': '/harvest_souls harvest', 'desc': 'Queue capability clusters for assimilation'},
            {'cmd': '/harvest_souls harvest all', 'desc': 'Queue all recommended capability clusters'},
            {'cmd': '/harvest_souls plan', 'desc': 'Show implementation path for a raw ability'},
            {'cmd': '/harvest_souls adopt', 'desc': 'Legacy: mark raw abilities for adoption'},
            {'cmd': '/harvest_souls adopt all', 'desc': 'Legacy: adopt all raw discovered abilities'},
            {'cmd': '/harvest_souls roadmap', 'desc': 'Show adoption roadmap and progress'},
            {'cmd': '/harvest_souls status', 'desc': 'Show last scan summary'},
            {'cmd': '/harvest_souls hermes-agent', 'desc': 'Scan only hermes-agent'},
            {'cmd': '/voyage dispatch', 'desc': 'Dispatch a task to a remote agent worker'},
            {'cmd': '/voyage status', 'desc': 'Check status of a voyage'},
            {'cmd': '/voyage list', 'desc': 'List recent voyages'},
            {'cmd': '/fleet setup', 'desc': 'Set up a remote agent team (install, auth, start agents)'},
            {'cmd': '/fleet status', 'desc': 'Show fleet status'},
        ]

    def _get_suggestions(self, prefix: str) -> list[dict]:
        """Get matching commands for a prefix."""
        prefix = prefix.strip().lower()
        catalog = self._command_catalog()
        if not prefix or prefix == '/':
            return catalog

        starts = [c for c in catalog if c['cmd'].lower().startswith(prefix)]
        if starts:
            return starts[:30]

        token_matches: list[dict] = []
        needle = prefix.lstrip('/')
        for item in catalog:
            cmd = item['cmd'].lower()
            parts = [p for p in cmd.replace('/', ' ').replace('<', ' ').replace('>', ' ').split() if p]
            if any(part.startswith(needle) for part in parts):
                token_matches.append(item)
        if token_matches:
            return token_matches[:10]

        return []

    # First-token dispatch table: maps the first whitespace-separated token
    # of a command line to the name of the ``_cmd_*`` method that owns that
    # command family. Dispatching on the exact first token means prefix
    # families with subcommands ('/setup provider ...', '/automate list')
    # and near-collisions ('/resume' vs '/resume-room', '/idea' vs '/ideas'
    # vs '/idea-detail') resolve exactly as the original longest-match
    # if/elif chain did, because the family name is always a whole token.
    _COMMAND_HANDLERS: dict[str, str] = {
        # core (CoreCommandsMixin)
        '/help': '_cmd_help',
        '/?': '_cmd_help',
        '/setup': '_cmd_setup',
        '/model': '_cmd_model',
        '/models': '_cmd_models',
        '/provider': '_cmd_provider',
        '/settings': '_cmd_settings',
        '/config': '_cmd_settings',
        '/reset': '_cmd_reset',
        '/resume': '_cmd_resume',
        '/hotkeys': '_cmd_hotkeys',
        '/timestamps': '_cmd_timestamps',
        '/interrupt': '_cmd_interrupt',
        '/abort': '_cmd_interrupt',
        '/thoughts': '_cmd_thoughts',
        '/tools': '_cmd_tools',
        '/history': '_cmd_history',
        '/consolidation': '_cmd_consolidation',
        # agents (AgentCommandsMixin)
        '/specialist': '_cmd_specialist',
        '/hermes': '_cmd_hermes_pi',
        '/pi': '_cmd_hermes_pi',
        '/fleet': '_cmd_fleet',
        '/voyage': '_cmd_voyage',
        '/add-remote': '_cmd_add_remote',
        '/harvest_souls': '_cmd_harvest_souls',
        '/shades': '_cmd_shades',
        '/shade': '_cmd_shades',
        # rooms (RoomCommandsMixin)
        '/conversation': '_cmd_conversation',
        '/team': '_cmd_team',
        '/devteam': '_cmd_devteam',
        '/pause-room': '_cmd_pause_room',
        '/resume-room': '_cmd_resume_room',
        '/say-room': '_cmd_say_room',
        '/inject-room': '_cmd_inject_room',
        '/delete-room': '_cmd_delete_room',
        # work (WorkCommandsMixin)
        '/clarifications': '_cmd_clarifications',
        '/clarify': '_cmd_clarify',
        '/idea': '_cmd_idea',
        '/ideas': '_cmd_ideas',
        '/idea-detail': '_cmd_idea_detail',
        '/libris': '_cmd_libris',
        '/devop': '_cmd_devop',
        '/monitor': '_cmd_monitor',
        '/automate': '_cmd_automate',
        '/project': '_cmd_project',
        '/autonomous': '_cmd_autonomous',
        '/confirm': '_cmd_confirm',
        '/reject': '_cmd_reject',
        '/approve': '_cmd_approve',
        '/batch': '_cmd_batch',
    }

    def handle_command(self, command: str, request_id: str | None):
        """Handle /setup and other slash commands."""
        command = command.strip()
        if not command:
            return

        try:
            if self._pending_provider_switch and command in ('/1', '/2'):
                pending = self._pending_provider_switch
                self._pending_provider_switch = None
                if command == '/1':
                    self._switch_provider_with_transfer(str(pending.get('target_provider') or ''), request_id)
                else:
                    self._switch_provider_fresh(str(pending.get('target_provider') or ''), request_id)
                return

            # /fleet setup interactive flow — handle pending responses
            if self._pending_fleet_setup:
                self._handle_fleet_setup_response(command, request_id)
                return

            handler_name = self._COMMAND_HANDLERS.get(command.split(None, 1)[0])
            if handler_name is not None and getattr(self, handler_name)(command, request_id) is not UNHANDLED:
                return

            # Unknown command — show suggestions
            suggestions = self._get_suggestions(command)
            if suggestions:
                common.emit({
                    'type': 'suggestions',
                    'title': 'Did you mean?',
                    'items': suggestions,
                    'request_id': request_id,
                })
            else:
                common.emit({'type': 'error', 'error': f'Unknown command: {command}', 'request_id': request_id})
        except Exception as e:
            common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
