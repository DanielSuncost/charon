/**
 * Dashboard view — two-row pane layout.
 *
 * Top row: Agents
 *   Left:   Agent list (Select, arrow keys, Enter to open session)
 *   Middle: Agent detail (description, goal, status)
 *   Right:  Recent activity (rear-view mirror)
 *
 * Bottom row: Projects
 *   Left:   Project list (Select, arrow keys, Enter to open session grid)
 *   Middle: Project description
 *   Right:  Agents working on this project
 *
 * Tab switches focus between agent list and project list.
 */

import {
  Box, Text, Select, SelectRenderableEvents,
  t, fg, bold, dim, cyan, green, yellow, red,
  type StyledText, StyledText as StyledTextClass,
  type SelectOption,
} from '@opentui/core'

export interface Agent {
  id: string
  name: string
  status: string
  role: string
  specialization: string
  goal: string
  project: string
  mode: string
  last_active?: string
}

export interface Project {
  name: string
  path: string
  agents: string[]
  last_active?: string
}

export interface DashboardCallbacks {
  onAgentSelect: (agentId: string) => void
  onProjectSelect: (projectName: string) => void
}

function statusIcon(status: string): string {
  switch (status) {
    case 'running': return '●'
    case 'idle': return '○'
    case 'stopped': return '✖'
    case 'error': return '✖'
    default: return '·'
  }
}

function statusColor(status: string): string {
  switch (status) {
    case 'running': return '#22c55e'
    case 'idle': return '#6b7280'
    case 'stopped': return '#ef4444'
    case 'error': return '#ef4444'
    default: return '#6b7280'
  }
}

export function createDashboard(callbacks: DashboardCallbacks) {
  // ── Agent section ──────────────────────────────────────────────────────

  const agentSelect = Select({
    options: [{ name: 'No agents', description: 'Create one with /agent create', value: null }],
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

  const agentDetail = Text({
    content: t`${dim('  Select an agent to view details')}`,
    width: '100%',
  })

  const agentActivity = Text({
    content: t`${dim('  No recent activity')}`,
    width: '100%',
  })

  agentSelect.on(SelectRenderableEvents.SELECTION_CHANGED, () => {
    const opt = agentSelect.getSelectedOption()
    if (opt?.value) {
      const a = opt.value as Agent
      const lines = [
        t`${bold(fg(statusColor(a.status))(`${statusIcon(a.status)} ${a.name}`))}`,
        t`${dim('  ID:      ')}${a.id}`,
        t`${dim('  Role:    ')}${a.specialization || a.role}`,
        t`${dim('  Mode:    ')}${a.mode}`,
        t`${dim('  Status:  ')}${fg(statusColor(a.status))(a.status)}`,
        t`${dim('  Goal:    ')}${a.goal || '—'}`,
        t`${dim('  Project: ')}${a.project || '—'}`,
      ]
      agentDetail.content = lines.reduce((a, b) => t`${a}\n${b}`)
    }
  })

  agentSelect.on(SelectRenderableEvents.ITEM_SELECTED, () => {
    const opt = agentSelect.getSelectedOption()
    if (opt?.value) {
      callbacks.onAgentSelect((opt.value as Agent).id)
    }
  })

  // ── Project section ────────────────────────────────────────────────────

  const projectSelect = Select({
    options: [{ name: 'No projects', description: 'Agents will create project entries', value: null }],
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

  const projectDetail = Text({
    content: t`${dim('  Select a project to view details')}`,
    width: '100%',
  })

  const projectAgents = Text({
    content: t`${dim('  No agents assigned')}`,
    width: '100%',
  })

  projectSelect.on(SelectRenderableEvents.SELECTION_CHANGED, () => {
    const opt = projectSelect.getSelectedOption()
    if (opt?.value) {
      const p = opt.value as Project
      projectDetail.content = t`${bold(cyan(p.name))}\n${dim('  Path: ')}${p.path}\n${dim('  Active: ')}${p.last_active || '—'}`
      if (p.agents.length) {
        const lines = p.agents.map(a => t`${dim('  • ')}${a}`)
        projectAgents.content = lines.reduce((a, b) => t`${a}\n${b}`)
      } else {
        projectAgents.content = t`${dim('  No agents assigned')}`
      }
    }
  })

  projectSelect.on(SelectRenderableEvents.ITEM_SELECTED, () => {
    const opt = projectSelect.getSelectedOption()
    if (opt?.value) {
      callbacks.onProjectSelect((opt.value as Project).name)
    }
  })

  // ── Layout ─────────────────────────────────────────────────────────────

  const layout = Box(
    { flexDirection: 'column', flexGrow: 1, width: '100%', height: '100%', gap: 1 },

    // Top row: Agents
    Box(
      { flexDirection: 'row', flexGrow: 1, width: '100%', gap: 1 },
      // Left: agent list
      Box(
        {
          flexBasis: '30%', borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', overflow: 'hidden',
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Agents'))}`, width: '100%' }),
        agentSelect,
      ),
      // Middle: agent detail
      Box(
        {
          flexGrow: 1, borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', paddingLeft: 1, paddingRight: 1,
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Detail'))}`, width: '100%' }),
        agentDetail,
      ),
      // Right: activity
      Box(
        {
          flexBasis: '30%', borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', paddingLeft: 1, paddingRight: 1, overflow: 'hidden',
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Activity'))}`, width: '100%' }),
        agentActivity,
      ),
    ),

    // Bottom row: Projects
    Box(
      { flexDirection: 'row', flexGrow: 1, width: '100%', gap: 1 },
      // Left: project list
      Box(
        {
          flexBasis: '30%', borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', overflow: 'hidden',
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Projects'))}`, width: '100%' }),
        projectSelect,
      ),
      // Middle: project detail
      Box(
        {
          flexGrow: 1, borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', paddingLeft: 1, paddingRight: 1,
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Project Detail'))}`, width: '100%' }),
        projectDetail,
      ),
      // Right: project agents
      Box(
        {
          flexBasis: '30%', borderStyle: 'rounded', borderColor: '#3b3252',
          flexDirection: 'column', paddingLeft: 1, paddingRight: 1, overflow: 'hidden',
        },
        Text({ content: t`${bold(fg('#a78bfa')(' Assigned Agents'))}`, width: '100%' }),
        projectAgents,
      ),
    ),
  )

  // ── Update functions ───────────────────────────────────────────────────

  function updateAgents(agents: Agent[]) {
    if (!agents.length) {
      agentSelect.setOptions([{ name: 'No agents', description: 'Create one with /agent create', value: null }])
      return
    }
    agentSelect.setOptions(agents.map(a => ({
      name: `${statusIcon(a.status)} ${a.name}`,
      description: `${a.specialization || a.role} · ${a.status}`,
      value: a,
    })))
  }

  function updateProjects(projects: Project[]) {
    if (!projects.length) {
      projectSelect.setOptions([{ name: 'No projects', description: 'Agents will create project entries', value: null }])
      return
    }
    projectSelect.setOptions(projects.map(p => ({
      name: p.name,
      description: `${p.agents.length} agent${p.agents.length !== 1 ? 's' : ''}`,
      value: p,
    })))
  }

  function updateActivity(lines: string[]) {
    if (!lines.length) {
      agentActivity.content = t`${dim('  No recent activity')}`
      return
    }
    const styled = lines.map(l => t`${dim(`  ${l}`)}`)
    agentActivity.content = styled.reduce((a, b) => t`${a}\n${b}`)
  }

  function focusAgents() { agentSelect.focus() }
  function focusProjects() { projectSelect.focus() }

  return {
    layout,
    updateAgents,
    updateProjects,
    updateActivity,
    focusAgents,
    focusProjects,
    agentSelect,
    projectSelect,
  }
}
