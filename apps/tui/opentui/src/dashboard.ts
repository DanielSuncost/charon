/**
 * Dashboard — real multi-column layout using instantiated renderables.
 *
 * Two rows × three columns:
 *
 * ┌─ Agents ──────┬─ Info / Goal ─────┬─ Rearview ────────┐
 * │ [filter]       │ Agent Info        │ Recent actions:   │
 * │ ▸ ● scout      │ ─────────────    │ • read main.py    │
 * │   ○ archivist  │ Goal + progress   │ • edited config   │
 * │   ● test       │ [token meter]     │ • ran tests       │
 * ├─ Projects ────┼─ Project Info ────┼─ Project Agents ──┤
 * │ ▸ ◉ charon    │ Path, metrics     │ • scout (running) │
 * │   ◎ demo      │ [token meter]     │ • test (idle)     │
 * └───────────────┴──────────────────┴───────────────────┘
 */

import {
  Box, Text, ScrollBox, instantiate,
  t, fg, bold, dim, green, cyan, red, yellow,
  type StyledText, StyledText as SC,
  type TextChunk,
} from '@opentui/core'

// Re-export joinStyled from index for reuse
function joinStyled(...parts: (StyledText | string)[]): StyledText {
  const chunks: TextChunk[] = []
  for (const p of parts) {
    if (typeof p === 'string') chunks.push({ __isChunk: true, text: p } as TextChunk)
    else if (p && (p as any).chunks) chunks.push(...(p as any).chunks)
  }
  return new SC(chunks)
}

const ic = (s: string) => s === 'running' ? '●' : s === 'idle' ? '○' : s === 'stopped' ? '✖' : '·'
const sc = (s: string) => s === 'running' ? '#22c55e' : s === 'stopped' ? '#ef4444' : '#6b7280'
const BORDER = '#3b3252'
const ACCENT = '#a78bfa'
const HEADER_BG = '#1a1730'

interface Agent {
  id: string; name: string; status: string; role: string; goal: string
  project: string; mode: string; visibility: string; last_active: string
  recent_actions: string[]; last_summary: string; memory_notes: number
  parent_agent_id: string
}
interface Project {
  name: string; path: string; agents: string[]; agent_details: Array<{name: string; id: string; status: string; role: string}>
  last_active: string; started: string; active: boolean
}

export interface DashboardState {
  agents: Agent[]
  projects: Project[]
  activity: string[]
  section: 'agents' | 'projects'
  agentIdx: number
  projectIdx: number
  agentFilter: { charon: boolean; shade: boolean; external: boolean }
}

export function createDashboardState(): DashboardState {
  return {
    agents: [], projects: [], activity: [],
    section: 'agents', agentIdx: 0, projectIdx: 0,
    agentFilter: { charon: true, shade: false, external: true },
  }
}

export function filteredAgents(ds: DashboardState): Agent[] {
  return ds.agents.filter(a => {
    // Hide stopped shades entirely (they're ephemeral workers)
    if (a.role === 'shade' && a.status === 'stopped') return false
    if (a.visibility === 'internal' && !ds.agentFilter.shade) return false
    if (a.visibility === 'background' && !ds.agentFilter.shade) return false
    if (a.role === 'shade' && !ds.agentFilter.shade) return false
    if (a.role === 'charon' && !ds.agentFilter.charon) return false
    if (a.role === 'external' && !ds.agentFilter.external) return false
    return true
  })
}

/**
 * Create the dashboard Box tree. Returns the root Box and update function.
 * The root Box contains all 6 cells, each with their own Text renderable.
 */
export function createDashboardLayout(renderer: any) {
  // Create all 6 text cells as real instances
  const agentListText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const agentInfoText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const agentRearText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const projListText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const projInfoText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const projAgentsText = instantiate(renderer, Text({ content: '', width: '100%' })) as any

  // Build the Box tree
  // Helper: wrap a Text in a bordered Box with internal ScrollBox
  function cell(text: any, widthPct: string | undefined, grow?: boolean) {
    const scrollInner = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%' })) as any
    scrollInner.add(text)
    const opts: any = {
      borderStyle: 'rounded', borderColor: BORDER,
      flexDirection: 'column', overflow: 'hidden', paddingLeft: 1,
    }
    if (widthPct) opts.width = widthPct
    if (grow) opts.flexGrow = 1
    const box = instantiate(renderer, Box(opts)) as any
    box.add(scrollInner)
    return box
  }

  const agentListBox = cell(agentListText, '28%')
  const agentInfoBox = cell(agentInfoText, undefined, true)
  const agentRearBox = cell(agentRearText, '28%')

  const agentRow = instantiate(renderer, Box({
    flexGrow: 1, width: '100%', flexDirection: 'row',
  })) as any
  agentRow.add(agentListBox)
  agentRow.add(agentInfoBox)
  agentRow.add(agentRearBox)

  const projListBox = cell(projListText, '28%')
  const projInfoBox = cell(projInfoText, undefined, true)
  const projAgentsBox = cell(projAgentsText, '28%')

  const projRow = instantiate(renderer, Box({
    flexGrow: 1, width: '100%', flexDirection: 'row',
  })) as any
  projRow.add(projListBox)
  projRow.add(projInfoBox)
  projRow.add(projAgentsBox)

  const root = instantiate(renderer, Box({
    flexGrow: 1, width: '100%', height: '100%', flexDirection: 'column',
  })) as any
  root.add(agentRow)
  root.add(projRow)

  // ── Update function ────────────────────────────────────────────────

  function update(ds: DashboardState) {
    const agents = filteredAgents(ds)
    const selAgent = ds.section === 'agents' && agents[ds.agentIdx] ? agents[ds.agentIdx] : null

    // Agent list
    const alParts: (StyledText | string)[] = []
    const listHeader = ds.section === 'agents'
      ? t`${bold(fg(ACCENT)('▸ Agents'))}`
      : t`${dim('Agents')}`
    alParts.push(listHeader)

    // Filter indicator
    const filters: string[] = []
    if (ds.agentFilter.charon) filters.push('charon')
    if (ds.agentFilter.shade) filters.push('shade')
    if (ds.agentFilter.external) filters.push('ext')
    alParts.push('\n')
    alParts.push(t`${dim(`[${filters.join('|')}] f:filter`)}`)

    if (agents.length === 0) {
      alParts.push('\n\n')
      alParts.push(t`${dim('(no agents)')}`)
    } else {
      for (let i = 0; i < agents.length; i++) {
        const a = agents[i]
        const sel = ds.section === 'agents' && i === ds.agentIdx
        alParts.push('\n')
        const spec = (a as any).specialization
        const specLabel = spec ? t`${dim(` (${spec})`)}` : ''
        const idLabel = a.id ? t`${dim(` ${a.id}`)}` : ''
        if (sel) {
          alParts.push(joinStyled(
            t`${bold(fg(ACCENT)('▸ '))}`,
            t`${fg(sc(a.status))(`${ic(a.status)} `)}`,
            t`${bold(fg('#f8fafc')(a.name))}`,
            idLabel,
            specLabel,
          ))
        } else {
          alParts.push(joinStyled(
            '  ',
            t`${fg(sc(a.status))(`${ic(a.status)} `)}`,
            t`${fg('#9ca3af')(a.name)}`,
            idLabel,
            specLabel,
          ))
        }
      }
    }
    agentListText.content = joinStyled(...alParts)

    // Agent info + goal (middle column)
    const aiParts: (StyledText | string)[] = []
    if (selAgent) {
      // Info section
      aiParts.push(t`${bold(fg(ACCENT)('Agent Info'))}`)
      aiParts.push('\n')
      aiParts.push(joinStyled(t`${dim('ID:     ')}`, t`${selAgent.id}`))
      aiParts.push('\n')
      const specText = (selAgent as any).specialization
      if (specText) {
        aiParts.push(joinStyled(t`${dim('Role:   ')}`, t`${bold(fg('#d4a44a')(specText))}`, t`${dim('  Mode: ')}`, t`${selAgent.mode}`))
      } else {
        aiParts.push(joinStyled(t`${dim('Role:   ')}`, t`${selAgent.role}`, t`${dim('  Mode: ')}`, t`${selAgent.mode}`))
      }
      aiParts.push('\n')
      aiParts.push(joinStyled(t`${dim('Status: ')}`, t`${fg(sc(selAgent.status))(selAgent.status)}`))
      aiParts.push('\n')
      aiParts.push(joinStyled(t`${dim('Project:')}`, t`${(selAgent.project || '—').split('/').pop() || '—'}`))
      aiParts.push('\n')
      aiParts.push(joinStyled(t`${dim('Memory: ')}`, t`${selAgent.memory_notes} notes`))

      // Divider
      aiParts.push('\n\n')
      aiParts.push(t`${fg(BORDER)('────────────────────────────')}`)

      // Goal section
      aiParts.push('\n')
      aiParts.push(t`${bold(fg(ACCENT)('Goal'))}`)
      aiParts.push('\n')
      aiParts.push(t`${selAgent.goal || dim('(no goal set)')}`)

      // Token meter (placeholder)
      aiParts.push('\n\n')
      aiParts.push(t`${dim('Tokens: ')}`)
      const tokenBar = 6 // placeholder
      aiParts.push(t`${fg('#22c55e')('█'.repeat(tokenBar))}${fg('#374151')('░'.repeat(20 - tokenBar))}`)
      aiParts.push(t`${dim(' ~' + (tokenBar * 1000) + ' tok')}`)

      // Last summary
      if (selAgent.last_summary) {
        aiParts.push('\n\n')
        aiParts.push(t`${dim('Last: ')}`)
        aiParts.push(t`${dim(selAgent.last_summary)}`)
      }
    } else {
      aiParts.push(t`${dim('← Select an agent')}`)
    }
    agentInfoText.content = joinStyled(...aiParts)

    // Agent rearview (right column)
    const arParts: (StyledText | string)[] = []
    arParts.push(t`${bold(fg(ACCENT)('Rearview'))}`)
    if (selAgent && selAgent.recent_actions.length > 0) {
      for (const action of selAgent.recent_actions.slice(-10)) {
        arParts.push('\n')
        arParts.push(t`${dim('• ' + action)}`)
      }
    } else if (selAgent) {
      arParts.push('\n')
      arParts.push(t`${dim('(no recent actions)')}`)
    } else {
      arParts.push('\n')
      arParts.push(t`${dim('← Select an agent')}`)
    }
    // Add global activity if no agent selected
    if (!selAgent && ds.activity.length > 0) {
      arParts.push('\n\n')
      arParts.push(t`${dim('System activity:')}`)
      for (const a of ds.activity.slice(-8)) {
        arParts.push('\n')
        arParts.push(t`${dim(a)}`)
      }
    }
    agentRearText.content = joinStyled(...arParts)

    // ── Projects row ──────────────────────────────────────────

    const selProject = ds.section === 'projects' && ds.projects[ds.projectIdx]
      ? ds.projects[ds.projectIdx] : null

    // Project list
    const plParts: (StyledText | string)[] = []
    const plHeader = ds.section === 'projects'
      ? t`${bold(fg(ACCENT)('▸ Projects'))}`
      : t`${dim('Projects')}`
    plParts.push(plHeader)

    if (ds.projects.length === 0) {
      plParts.push('\n\n')
      plParts.push(t`${dim('(no projects)')}`)
      plParts.push('\n')
      plParts.push(t`${dim('/project new "name"')}`)
    } else {
      for (let i = 0; i < ds.projects.length; i++) {
        const p = ds.projects[i]
        const sel = ds.section === 'projects' && i === ds.projectIdx
        const actIcon = p.active ? '◉' : '◎'
        const actColor = p.active ? '#22c55e' : '#6b7280'
        plParts.push('\n')
        if (sel) {
          plParts.push(joinStyled(
            t`${bold(fg(ACCENT)('▸ '))}`,
            t`${fg(actColor)(actIcon + ' ')}`,
            t`${bold(fg('#f8fafc')(p.name))}`,
          ))
        } else {
          plParts.push(joinStyled(
            '  ',
            t`${fg(actColor)(actIcon + ' ')}`,
            t`${fg('#9ca3af')(p.name)}`,
          ))
        }
      }
    }
    projListText.content = joinStyled(...plParts)

    // Project info
    const piParts: (StyledText | string)[] = []
    if (selProject) {
      piParts.push(t`${bold(fg(ACCENT)('Project Info'))}`)
      piParts.push('\n')
      piParts.push(joinStyled(t`${dim('Name:    ')}`, t`${bold(selProject.name)}`))
      piParts.push('\n')
      piParts.push(joinStyled(t`${dim('Path:    ')}`, t`${selProject.path}`))
      piParts.push('\n')
      piParts.push(joinStyled(t`${dim('Started: ')}`, t`${selProject.started ? selProject.started.slice(0, 10) : '—'}`))
      piParts.push('\n')
      piParts.push(joinStyled(t`${dim('Active:  ')}`, selProject.active ? t`${fg('#22c55e')('● yes')}` : t`${dim('○ idle')}`))
      piParts.push('\n')
      piParts.push(joinStyled(t`${dim('Agents:  ')}`, t`${String(selProject.agents.length)}`))

      // Token meter placeholder
      piParts.push('\n\n')
      piParts.push(t`${dim('Token usage:')}`)
      piParts.push('\n')
      const tok = Math.min(20, selProject.agents.length * 3)
      piParts.push(joinStyled(
        t`${fg('#7c3aed')('█'.repeat(tok))}`,
        t`${fg('#374151')('░'.repeat(20 - tok))}`,
        t`${dim(` ~${tok * 2}k`)}`,
      ))
    } else {
      piParts.push(t`${dim('← Select a project')}`)
    }
    projInfoText.content = joinStyled(...piParts)

    // Project agents (right column)
    const paParts: (StyledText | string)[] = []
    paParts.push(t`${bold(fg(ACCENT)('Agents on Project'))}`)
    if (selProject && selProject.agent_details.length > 0) {
      for (const ad of selProject.agent_details) {
        paParts.push('\n')
        paParts.push(joinStyled(
          t`${fg(sc(ad.status))(`${ic(ad.status)} `)}`,
          t`${ad.name}`,
          t`${dim(` (${ad.role})`)}`,
        ))
      }
    } else if (selProject) {
      paParts.push('\n')
      paParts.push(t`${dim('(no agents assigned)')}`)
    } else {
      paParts.push('\n')
      paParts.push(t`${dim('← Select a project')}`)
    }
    projAgentsText.content = joinStyled(...paParts)
  }

  return { root, update }
}
