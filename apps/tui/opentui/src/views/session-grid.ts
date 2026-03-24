/**
 * Session Grid view — grid of terminal sessions.
 *
 * Shows all Charon-enabled agent sessions across locations.
 * Can be filtered by project. Tab between sessions, Enter to interact.
 *
 * For now this is a list-based view (actual terminal embedding comes with
 * charons-boat integration). Each session card shows:
 *   - Agent name and status
 *   - Project
 *   - Last activity snippet
 *   - Connection status (local / remote)
 */

import {
  Box, Text, Select, SelectRenderableEvents,
  t, fg, bold, dim, cyan, green, yellow, red,
  type StyledText,
} from '@opentui/core'

export interface Session {
  id: string
  agentId: string
  agentName: string
  status: string
  project: string
  location: string  // 'local' or hostname
  lastActivity: string
  tmuxSession?: string
}

export interface SessionGridCallbacks {
  onSessionSelect: (sessionId: string, agentId: string) => void
}

function statusIcon(status: string): string {
  switch (status) {
    case 'running': return '●'
    case 'idle': return '○'
    case 'stopped': return '✖'
    default: return '·'
  }
}

function statusColor(status: string): string {
  switch (status) {
    case 'running': return '#22c55e'
    case 'idle': return '#6b7280'
    case 'stopped': return '#ef4444'
    default: return '#6b7280'
  }
}

function locationIcon(location: string): string {
  return location === 'local' ? '⌂' : '⇄'
}

export function createSessionGrid(callbacks: SessionGridCallbacks) {
  // Filter display
  const filterText = Text({
    content: t`${dim('  Showing: all sessions')}`,
    width: '100%',
  })

  // Session list
  const sessionSelect = Select({
    options: [{ name: 'No active sessions', description: 'Start an agent to create a session', value: null }],
    backgroundColor: '#0f0f1a',
    textColor: '#9ca3af',
    focusedBackgroundColor: '#1e1b3a',
    focusedTextColor: '#f8fafc',
    selectedBackgroundColor: '#2d2655',
    selectedTextColor: '#ffffff',
    descriptionColor: '#6b7280',
    selectedDescriptionColor: '#a78bfa',
    showDescription: true,
    wrapSelection: true,
    flexGrow: 1,
    width: '100%',
  })

  // Session detail panel
  const sessionDetail = Text({
    content: t`${dim('  Select a session to view details')}`,
    width: '100%',
  })

  // Session output preview
  const sessionPreview = Text({
    content: t`${dim('  No output available')}`,
    width: '100%',
  })

  sessionSelect.on(SelectRenderableEvents.SELECTION_CHANGED, () => {
    const opt = sessionSelect.getSelectedOption()
    if (opt?.value) {
      const s = opt.value as Session
      const lines = [
        t`${bold(fg(statusColor(s.status))(`${statusIcon(s.status)} ${s.agentName}`))}`,
        t`${dim('  Session:  ')}${s.id}`,
        t`${dim('  Agent:    ')}${s.agentId}`,
        t`${dim('  Status:   ')}${fg(statusColor(s.status))(s.status)}`,
        t`${dim('  Project:  ')}${s.project || '—'}`,
        t`${dim('  Location: ')}${locationIcon(s.location)} ${s.location}`,
        t`${dim('  tmux:     ')}${s.tmuxSession || '—'}`,
        t`${dim('  Activity: ')}${s.lastActivity || '—'}`,
      ]
      sessionDetail.content = lines.reduce((a, b) => t`${a}\n${b}`)
    }
  })

  sessionSelect.on(SelectRenderableEvents.ITEM_SELECTED, () => {
    const opt = sessionSelect.getSelectedOption()
    if (opt?.value) {
      const s = opt.value as Session
      callbacks.onSessionSelect(s.id, s.agentId)
    }
  })

  // Layout: two columns — session list on left, detail + preview on right
  const layout = Box(
    { flexDirection: 'column', flexGrow: 1, width: '100%', height: '100%' },

    // Filter bar
    Box(
      { width: '100%', backgroundColor: '#1e1b2e' },
      filterText,
    ),

    // Main content
    Box(
      { flexDirection: 'row', flexGrow: 1, width: '100%', gap: 1 },

      // Left: session list
      Box(
        {
          flexBasis: '40%', borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', overflow: 'hidden',
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Sessions'))}`, width: '100%' }),
        sessionSelect,
      ),

      // Right: detail + preview
      Box(
        { flexGrow: 1, flexDirection: 'column', gap: 1 },

        // Detail
        Box(
          {
            flexGrow: 1, borderStyle: 'rounded', borderColor: '#3b3252',
            flexDirection: 'column', paddingLeft: 1, paddingRight: 1,
          },
          Text({ content: t`${bold(fg('#a78bfa')(' Session Detail'))}`, width: '100%' }),
          sessionDetail,
        ),

        // Preview (placeholder for future terminal embed)
        Box(
          {
            flexGrow: 1, borderStyle: 'rounded', borderColor: '#3b3252',
            flexDirection: 'column', paddingLeft: 1, paddingRight: 1, overflow: 'hidden',
          },
          Text({ content: t`${bold(fg('#a78bfa')(' Output Preview'))}`, width: '100%' }),
          sessionPreview,
        ),
      ),
    ),
  )

  // ── Update functions ───────────────────────────────────────────────────

  function updateSessions(sessions: Session[], filterProject?: string) {
    let filtered = sessions
    if (filterProject) {
      filtered = sessions.filter(s => s.project === filterProject)
      filterText.content = t`${dim(`  Showing: ${filterProject} (${filtered.length} sessions)`)}`
    } else {
      filterText.content = t`${dim(`  Showing: all sessions (${filtered.length})`)}`
    }

    if (!filtered.length) {
      sessionSelect.setOptions([{
        name: 'No active sessions',
        description: filterProject ? `No sessions for ${filterProject}` : 'Start an agent to create a session',
        value: null,
      }])
      return
    }

    sessionSelect.setOptions(filtered.map(s => ({
      name: `${statusIcon(s.status)} ${locationIcon(s.location)} ${s.agentName}`,
      description: `${s.project || '—'} · ${s.status} · ${s.location}`,
      value: s,
    })))
  }

  function focus() { sessionSelect.focus() }

  return {
    layout,
    updateSessions,
    focus,
    sessionSelect,
  }
}
