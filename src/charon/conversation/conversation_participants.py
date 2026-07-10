from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ConversationParticipantSpec:
    id: str
    name: str
    role: str
    agent_type: str
    provider: str = ''
    model: str = ''
    transport: str = ''
    session: str = ''
    socket: str = ''
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationRuntimeCapabilities:
    can_spawn: bool = True
    supports_streaming: bool = True
    supports_structured_completion: bool = False
    supports_tools: bool = False
    native_boat: bool = False


class ConversationParticipantAdapter:
    agent_type = 'unknown'
    display_name = 'Unknown'
    capabilities = ConversationRuntimeCapabilities()

    def build_participant(self, seed: dict[str, Any]) -> ConversationParticipantSpec:
        return ConversationParticipantSpec(
            id=str(seed.get('id') or ''),
            name=str(seed.get('name') or seed.get('id') or self.display_name),
            role=str(seed.get('role') or 'participant'),
            agent_type=self.agent_type,
            provider=str(seed.get('provider') or self.agent_type),
            model=str(seed.get('model') or ''),
            transport=str(seed.get('transport') or ''),
            session=str(seed.get('session') or ''),
            socket=str(seed.get('socket') or ''),
            meta=dict(seed.get('meta') or {}),
        )

    def spawn_command(self, *, project_root: Path, session_name: str, participant: ConversationParticipantSpec) -> list[str]:
        raise NotImplementedError


class BoatWrappedCliAdapter(ConversationParticipantAdapter):
    command: list[str] = []

    def spawn_command(self, *, project_root: Path, session_name: str, participant: ConversationParticipantSpec) -> list[str]:
        boat = project_root / 'tools' / 'charons-boat' / 'charons-boat'
        return [str(boat), 'wrap', '--name', session_name, '--', *self.command]


class HermesConversationAdapter(BoatWrappedCliAdapter):
    agent_type = 'hermes'
    display_name = 'Hermes'
    capabilities = ConversationRuntimeCapabilities(can_spawn=True, supports_streaming=True, supports_structured_completion=False, supports_tools=False, native_boat=False)
    command = ['hermes']


class PiConversationAdapter(BoatWrappedCliAdapter):
    agent_type = 'pi'
    display_name = 'pi'
    capabilities = ConversationRuntimeCapabilities(can_spawn=True, supports_streaming=True, supports_structured_completion=False, supports_tools=False, native_boat=False)
    command = ['pi']


class CharonConversationAdapter(ConversationParticipantAdapter):
    agent_type = 'charon'
    display_name = 'Charon'
    capabilities = ConversationRuntimeCapabilities(can_spawn=False, supports_streaming=True, supports_structured_completion=False, supports_tools=True, native_boat=True)

    def spawn_command(self, *, project_root: Path, session_name: str, participant: ConversationParticipantSpec) -> list[str]:
        raise NotImplementedError('Charon conversation participants require a native adapter/session path; spawn command not wired yet.')


_ADAPTERS: dict[str, ConversationParticipantAdapter] = {
    'hermes': HermesConversationAdapter(),
    'pi': PiConversationAdapter(),
    'charon': CharonConversationAdapter(),
}


def get_conversation_adapter(agent_type: str) -> ConversationParticipantAdapter | None:
    return _ADAPTERS.get(str(agent_type or '').strip().lower())


def supported_conversation_agent_types() -> list[str]:
    return sorted(_ADAPTERS.keys())
