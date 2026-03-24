/**
 * Sessions view — agent session grid with project/agent filtering.
 *
 * Layout:
 * ┌─ Agents (28%) ────┬─ Session Grid (72%) ─────────────────────┐
 * │ [✓] ● scout       │ ╭─ scout ──────╮╭─ test ───────╮        │
 * │ [ ] ○ archivist   │ │              ││              │        │
 * │ [✓] ● test        │ │  scout       ││  test        │        │
 * │                    │ │              ││              │        │
 * ├─ Projects ────────┤ │              ││              │        │
 * │ ▸ All Projects    │ ╰──────────────╯╰──────────────╯        │
 * │   charon          │                                          │
 * │   demo            │                                          │
 * └───────────────────┴──────────────────────────────────────────┘
 *
 * Controls:
 * - Tab: switch between agents pane and projects pane
 * - ↑↓: navigate lists
 * - Enter (agents): toggle agent visibility in grid
 * - Enter (projects): filter agents to this project
 * - F1: chat, F2: dashboard
 */

import {
  Box, Text, ScrollBox, instantiate,
  t, fg, bold, dim,
  type StyledText, StyledText as SC,
  type TextChunk,
} from '@opentui/core'

function joinStyled(...parts: (StyledText | string)[]): StyledText {
  const chunks: TextChunk[] = []
  for (const p of parts) {
    if (typeof p === 'string') chunks.push({ __isChunk: true, text: p } as TextChunk)
    else if (p && (p as any).chunks) chunks.push(...(p as any).chunks)
  }
  return new SC(chunks)
}

function stripAnsi(s: string): string {
  return s.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '').replace(/\x1b[^\x1b]/g, '')
}

/** Wrap lines to fit within a max width. Returns wrapped lines. */
function wrapLines(lines: string[], maxW: number): string[] {
  const result: string[] = []
  for (const line of lines) {
    const clean = stripAnsi(line)
    if (clean.length <= maxW) {
      result.push(clean)
    } else {
      // Hard wrap at maxW
      let remaining = clean
      while (remaining.length > 0) {
        result.push(remaining.slice(0, maxW))
        remaining = remaining.slice(maxW)
      }
    }
  }
  return result
}

const ic = (s: string) => s === 'running' ? '●' : s === 'idle' ? '○' : s === 'stopped' ? '✖' : s === 'waiting' ? '🔔' : '·'

// Session state colors:
//   active/running = gold (agent is working)
//   idle = grayblue (session open but not doing anything)
//   waiting = bright blue + bell (waiting for user confirmation)
//   stopped = dim red
const GOLD = '#d4a44a'
const GRAYBLUE = '#6b7f99'
const BRIGHT_BLUE = '#60a5fa'
const DIM_RED = '#6b3333'

function sessionBorderColor(status: string): string {
  if (status === 'running') return GOLD
  if (status === 'waiting') return BRIGHT_BLUE
  if (status === 'idle') return GRAYBLUE
  if (status === 'stopped') return DIM_RED
  return GRAYBLUE
}

function sessionSummaryColor(status: string): string {
  if (status === 'running') return GOLD
  if (status === 'waiting') return BRIGHT_BLUE
  return GRAYBLUE
}

const BORDER = '#3b3252'
const ACCENT = '#a78bfa'

interface Agent {
  id: string; name: string; status: string; role: string; project: string
  tmux_session?: string; tmuxSession?: string; hasTmux?: boolean; source?: string
  goal?: string; last_summary?: string
  specialization?: string  // user/agent-assigned role label, e.g. "infrastructure", "frontend"
}
interface Project {
  name: string; path: string; agents: string[]
}

export interface SessionsState {
  agents: Agent[]
  projects: Project[]
  section: 'agents' | 'projects' | 'grid'
  agentIdx: number
  projectIdx: number   // 0 = "All Projects", 1+ = actual projects
  gridIdx: number      // selected session in the grid
  visible: Set<string> // agent IDs currently visible in grid
  selectedProject: string | null  // null = all
  // Live tmux capture
  enteredSession: string | null   // agent ID currently "entered" (receiving input)
  zoomedSession: string | null    // agent ID currently zoomed (full screen, no other cells)
  tmuxContent: Map<string, string>  // agent ID → captured screen content
}

/** Get the visible agents in grid order */
export function gridAgents(ss: SessionsState): Agent[] {
  const filtered = projectAgents(ss)
  return filtered.filter(a => ss.visible.has(a.id))
}

export function createSessionsState(): SessionsState {
  return {
    agents: [], projects: [],
    section: 'agents', agentIdx: 0, projectIdx: 0, gridIdx: 0,
    visible: new Set(), selectedProject: null,
    enteredSession: null, zoomedSession: null, tmuxContent: new Map(),
  }
}

/** Get agents for the currently selected project filter */
function projectAgents(ss: SessionsState): Agent[] {
  if (!ss.selectedProject) return ss.agents.filter(a =>
    a.role !== 'shade' && (a.hasTmux || a.source === 'tmux' || a.source === 'virtual' || a.source === 'detected')
  )
  return ss.agents.filter(a => {
    if (a.role === 'shade') return false
    if (!a.hasTmux && a.source !== 'tmux' && a.source !== 'virtual' && a.source !== 'detected') return false
    const projName = (a.project || '').split('/').pop() || ''
    return projName === ss.selectedProject
  })
}

/** Sync visible set: when project changes, auto-show project's agents */
export function syncVisible(ss: SessionsState) {
  const agents = projectAgents(ss)
  // Keep existing visible agents that are still in the filtered list
  const validIds = new Set(agents.map(a => a.id))
  for (const id of ss.visible) {
    if (!validIds.has(id)) ss.visible.delete(id)
  }
  // If nothing visible, show all filtered agents
  if (ss.visible.size === 0) {
    for (const a of agents) ss.visible.add(a.id)
  }
}

export function createSessionsLayout(renderer: any) {
  // Left pane: agents list (top half) + projects list (bottom half)
  const agentListText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const projListText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const gridText = instantiate(renderer, Text({ content: '', width: '100%' })) as any

  // Agent list scroll
  const agentScroll = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%' })) as any
  agentScroll.add(agentListText)
  const agentBox = instantiate(renderer, Box({
    width: '100%', flexGrow: 1, borderStyle: 'rounded', borderColor: BORDER,
    flexDirection: 'column', overflow: 'hidden', paddingLeft: 1,
  })) as any
  agentBox.add(agentScroll)

  // Project list scroll
  const projScroll = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%' })) as any
  projScroll.add(projListText)
  const projBox = instantiate(renderer, Box({
    width: '100%', flexGrow: 1, borderStyle: 'rounded', borderColor: BORDER,
    flexDirection: 'column', overflow: 'hidden', paddingLeft: 1,
  })) as any
  projBox.add(projScroll)

  // Left column: agents on top, projects on bottom
  const leftCol = instantiate(renderer, Box({
    width: '28%', flexDirection: 'column', overflow: 'hidden',
  })) as any
  leftCol.add(agentBox)
  leftCol.add(projBox)

  // Grid pane (right side)
  const gridScroll = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%', height: '100%' })) as any
  gridScroll.add(gridText)
  const gridBox = instantiate(renderer, Box({
    flexGrow: 1,
    flexDirection: 'column', overflow: 'hidden', paddingLeft: 1,
  })) as any
  gridBox.add(gridScroll)

  // Root
  const root = instantiate(renderer, Box({
    flexGrow: 1, width: '100%', height: '100%', flexDirection: 'row',
  })) as any
  root.add(leftCol)
  root.add(gridBox)

  function update(ss: SessionsState, termW: number, termH?: number) {
    return  // TEMP: skip everything
    const terminalH = termH || 24
    const agents = projectAgents(ss)

    // ── Agent list ─────────────────────────────────────────────
    const alParts: (StyledText | string)[] = []
    alParts.push(ss.section === 'agents'
      ? t`${bold(fg(ACCENT)('▸ Agents'))}`
      : t`${dim('Agents')}`)
    alParts.push('\n')
    alParts.push(t`${dim('Enter: toggle visibility')}`)

    if (agents.length === 0) {
      alParts.push('\n\n')
      alParts.push(t`${dim('(no agents)')}`)
    } else {
      for (let i = 0; i < agents.length; i++) {
        const a = agents[i]
        const sel = ss.section === 'agents' && i === ss.agentIdx
        const checked = ss.visible.has(a.id)
        const checkbox = checked ? t`${fg('#22c55e')('[✓]')}` : t`${dim('[ ]')}`
        alParts.push('\n')
        if (sel) {
          alParts.push(joinStyled(
            t`${bold(fg(ACCENT)('▸'))} `,
            checkbox, ' ',
            t`${fg(sessionBorderColor(a.status))(`${ic(a.status)} `)}`,
            t`${bold(fg('#f8fafc')(a.name))}`,
          ))
        } else {
          alParts.push(joinStyled(
            '  ', checkbox, ' ',
            t`${fg(sessionBorderColor(a.status))(`${ic(a.status)} `)}`,
            t`${fg('#9ca3af')(a.name)}`,
          ))
        }
      }
    }
    // agentListText.content = joinStyled(...alParts)  // DISABLED

    // ── Project list ───────────────────────────────────────────
    const plParts: (StyledText | string)[] = []
    plParts.push(ss.section === 'projects'
      ? t`${bold(fg(ACCENT)('▸ Projects'))}`
      : t`${dim('Projects')}`)
    plParts.push('\n')
    plParts.push(t`${dim('Enter: filter agents')}`)

    // "All Projects" entry at index 0
    const allSel = ss.section === 'projects' && ss.projectIdx === 0
    const allActive = ss.selectedProject === null
    plParts.push('\n')
    if (allSel) {
      plParts.push(joinStyled(
        t`${bold(fg(ACCENT)('▸ '))}`,
        allActive ? t`${bold(fg('#f8fafc')('All Projects'))}` : t`${fg('#9ca3af')('All Projects')}`,
        allActive ? t`${fg(ACCENT)(' ●')}` : '',
      ))
    } else {
      plParts.push(joinStyled(
        '  ',
        allActive ? t`${bold('All Projects')}${fg(ACCENT)(' ●')}` : t`${fg('#9ca3af')('All Projects')}`,
      ))
    }

    for (let i = 0; i < ss.projects.length; i++) {
      const p = ss.projects[i]
      const sel = ss.section === 'projects' && i + 1 === ss.projectIdx
      const active = ss.selectedProject === p.name
      plParts.push('\n')
      if (sel) {
        plParts.push(joinStyled(
          t`${bold(fg(ACCENT)('▸ '))}`,
          active ? t`${bold(fg('#f8fafc')(p.name))}${fg(ACCENT)(' ●')}` : t`${fg('#9ca3af')(p.name)}`,
          t`${dim(` (${p.agents.length})`)}`,
        ))
      } else {
        plParts.push(joinStyled(
          '  ',
          active ? t`${bold(p.name)}${fg(ACCENT)(' ●')}` : t`${fg('#9ca3af')(p.name)}`,
          t`${dim(` (${p.agents.length})`)}`,
        ))
      }
    }
    // projListText.content = joinStyled(...plParts)  // DISABLED

    // ── Session grid ───────────────────────────────────────────
    const visibleAgents = agents.filter(a => ss.visible.has(a.id))
    const gParts: (StyledText | string)[] = []
    const gridFocused = ss.section === 'grid'

    const filterLabel = ss.selectedProject || 'All Projects'
    gParts.push(joinStyled(
      gridFocused ? t`${bold(fg(ACCENT)('▸ Session Grid'))}` : t`${bold(fg(ACCENT)('Session Grid'))}`,
      t`${dim(` — ${filterLabel} — ${visibleAgents.length} visible`)}`,
    ))
    if (gridFocused) {
      gParts.push('\n')
      if (ss.zoomedSession) {
        gParts.push(t`${dim('Esc: unzoom  Ctrl+F: toggle zoom  Type to interact')}`)
      } else if (ss.enteredSession) {
        gParts.push(t`${dim('Esc: exit  Ctrl+F: zoom full screen  Type to interact')}`)
      } else {
        gParts.push(t`${dim('↑↓←→: navigate  Enter: connect  Ctrl+F: zoom  Tab: lists')}`)
      }
    }

    if (visibleAgents.length === 0) {
      gParts.push('\n\n')
      gParts.push(t`${dim('No agents selected. Use Enter in the agents list to toggle visibility.')}`)
    } else {
      // Calculate grid dimensions — responsive to terminal size
      // Prefer 2 columns with wide cells for readability.
      // Only go to 3+ columns on very wide terminals.
      const gridW = Math.max(20, termW - Math.floor(termW * 0.28) - 6)
      const minCellH = 14
      const minCellW = 44
      const preferredCellW = 60

      // If a session is entered, give it most of the space
      const hasEntered = ss.enteredSession && visibleAgents.some(a => a.id === ss.enteredSession)

      let cellW: number
      let cellH: number
      let cols: number

      const isZoomed = ss.zoomedSession && visibleAgents.some(a => a.id === ss.zoomedSession)

      if (isZoomed) {
        // Zoomed: single session fills the entire grid
        cellW = Math.max(minCellW, gridW - 4)
        cellH = Math.max(minCellH, terminalH - 8)
        cols = 1
      } else if (hasEntered) {
        cellW = Math.max(minCellW, gridW - 4)
        cellH = Math.max(minCellH, terminalH - 10)
        cols = 1
      } else {
        const colsAtPreferred = Math.max(1, Math.floor(gridW / (preferredCellW + 2)))
        const colsAtMinimum = Math.max(1, Math.floor(gridW / (minCellW + 2)))
        cols = Math.min(colsAtPreferred || colsAtMinimum, visibleAgents.length)
        if (cols === 0) cols = 1
        cellW = Math.max(minCellW, Math.floor((gridW - (cols - 1)) / cols) - 2)
        const rows = Math.ceil(visibleAgents.length / cols)
        const availH = terminalH - 8
        cellH = Math.max(minCellH, Math.min(20, Math.floor(availH / rows) - 2))
      }

      // Clamp gridIdx
      if (ss.gridIdx >= visibleAgents.length) ss.gridIdx = Math.max(0, visibleAgents.length - 1)

      gParts.push('\n')

      // Render order: zoomed shows only one, entered shows it first + others compact
      const renderOrder = isZoomed
        ? visibleAgents.filter(a => a.id === ss.zoomedSession)
        : hasEntered
          ? [
              ...visibleAgents.filter(a => a.id === ss.enteredSession),
              ...visibleAgents.filter(a => a.id !== ss.enteredSession),
            ]
          : visibleAgents

      // Recalculate indices for render order
      const idToOrigIdx = new Map(visibleAgents.map((a, i) => [a.id, i]))

      for (let i = 0; i < renderOrder.length; i += cols) {
        const rowAgents = renderOrder.slice(i, i + cols)
        const rowIndices = rowAgents.map(a => idToOrigIdx.get(a.id) ?? 0)

        // Use big cell for entered session, evenly spread for others
        const isEnteredRow = hasEntered && rowAgents.some(a => a.id === ss.enteredSession)
        let thisCellW: number
        let thisCellH: number
        if (isEnteredRow) {
          thisCellW = Math.max(minCellW, gridW - 4)
          thisCellH = cellH
        } else {
          // Spread remaining cells evenly with wider preference
          const otherCount = renderOrder.filter(a => a.id !== ss.enteredSession).length
          const otherColsPref = Math.max(1, Math.floor(gridW / (preferredCellW + 2)))
          const otherCols = Math.min(otherColsPref || 1, otherCount)
          thisCellW = Math.max(minCellW, Math.floor((gridW - (otherCols - 1)) / otherCols) - 2)
          thisCellH = minCellH
        }

        // Summary line above each cell: [specialization] — current activity
        gParts.push('\n')
        for (let j = 0; j < rowAgents.length; j++) {
          if (j > 0) gParts.push(' ')
          const a = rowAgents[j]
          const spec = a.specialization ? `${a.specialization}` : ''
          const activity = a.last_summary || a.goal || (a.status === 'idle' ? 'idle' : a.status === 'running' ? 'working...' : a.status)
          const waitIcon = a.status === 'waiting' ? '🔔 ' : ''
          const summaryParts: (StyledText | string)[] = []
          if (spec) {
            summaryParts.push(t`${bold(fg(sessionSummaryColor(a.status))(spec))}`)
            summaryParts.push(t`${dim(' — ')}`)
          }
          summaryParts.push(t`${fg(sessionSummaryColor(a.status))(`${waitIcon}${activity}`.slice(0, thisCellW - spec.length - 5))}`)
          gParts.push(joinStyled(...summaryParts))
        }

        // Top border — colored by session state
        gParts.push('\n')
        for (let j = 0; j < rowAgents.length; j++) {
          if (j > 0) gParts.push(' ')
          const a = rowAgents[j]
          const isSelected = gridFocused && rowIndices[j] === ss.gridIdx
          const isEntered = ss.enteredSession === a.id
          const borderColor = isEntered ? '#22c55e' : isSelected ? ACCENT : sessionBorderColor(a.status)
          const statusIcon = isEntered ? '⏺' : a.status === 'waiting' ? '🔔' : ic(a.status)
          const label = ` ${statusIcon} ${a.name} `.slice(0, thisCellW - 2)
          const border = '─'.repeat(Math.max(0, thisCellW - label.length - 2))
          gParts.push(t`${fg(borderColor)(`╭─${label}${border}╮`)}`)
        }

        // Content rows — show tmux capture if available, otherwise agent info
        for (let row = 0; row < thisCellH; row++) {
          gParts.push('\n')
          for (let j = 0; j < rowAgents.length; j++) {
            if (j > 0) gParts.push(' ')
            const a = rowAgents[j]
            const isSelected = gridFocused && rowIndices[j] === ss.gridIdx
            const isEntered = ss.enteredSession === a.id
            const borderColor = isEntered ? '#22c55e' : isSelected ? ACCENT : sessionBorderColor(a.status)

            const tmuxKey = a.id
            const tmuxScreen = ss.tmuxContent.get(tmuxKey) || ''
            const rawLines = tmuxScreen ? tmuxScreen.split('\n') : []
            const contentW = thisCellW - 2
            const isExpanded = ss.enteredSession === a.id || ss.zoomedSession === a.id

            // Expanded/zoomed: show raw tmux capture (full TUI for interaction)
            // Grid overview: show clean summary text only
            const meaningfulLines: string[] = []
            if (isExpanded) {
              // Raw capture — wrap to fit cell
              for (const line of rawLines) {
                meaningfulLines.push(stripAnsi(line))
              }
            } else {
              // Summary mode — extract only conversation text
              for (const line of rawLines) {
                const clean = stripAnsi(line).trim()
                if (!clean) continue
                const words = clean.match(/[a-zA-Z]{3,}/g)
                if (!words || words.length === 0) continue
                if (/Type a message|\/command|\/setup/i.test(clean)) continue
                if (/F[123]:|Ctrl\+|PgUp/i.test(clean)) continue
                if (/^AG-\d+|interactive|effort:|ctx:\d|provider/i.test(clean)) continue
                if (/^\(no.?tmux/i.test(clean)) continue
                const stripped = clean.replace(/^[\s│┃|╭╰╮╯├┤─═►▸●○\[\]✓]+/, '').trim()
                if (stripped.length < 3) continue
                meaningfulLines.push(stripped)
              }
            }
            const wrappedAll = wrapLines(meaningfulLines, contentW)
            // Trim trailing empty lines
            while (wrappedAll.length > 0 && wrappedAll[wrappedAll.length - 1].trim() === '') wrappedAll.pop()
            const wrapStart = Math.max(0, wrappedAll.length - thisCellH)
            const tmuxLines = wrappedAll.slice(wrapStart)

            let content = ''
            if (tmuxLines.length > 0) {
              content = (tmuxLines[row] || '').slice(0, contentW)
            } else {
              // No tmux content — show agent info
              if (row === 0) content = ` ${ic(a.status)} ${a.status}`
              else if (row === 1) content = ` ${a.role}`
              else if (row === 2) content = ` ${(a.project || '').split('/').pop() || '—'}`
              else if (row === 3) content = ''
              else if (row === 4) content = tmuxLines.length ? '' : ' (no tmux session)'
              else content = ''
            }
            content = content.slice(0, contentW)
            content += ' '.repeat(Math.max(0, contentW - content.length))

            if (isEntered) {
              // Green border for entered/active session
              gParts.push(joinStyled(t`${fg(borderColor)('│')}`, t`${fg('#d1fae5')(content)}`, t`${fg(borderColor)('│')}`))
            } else if (isSelected && row === 0) {
              gParts.push(joinStyled(t`${fg(borderColor)('│')}`, t`${bold(fg('#f8fafc')(content))}`, t`${fg(borderColor)('│')}`))
            } else {
              gParts.push(joinStyled(t`${fg(borderColor)('│')}`, t`${dim(content)}`, t`${fg(borderColor)('│')}`))
            }
          }
        }

        // Bottom border
        gParts.push('\n')
        for (let j = 0; j < rowAgents.length; j++) {
          if (j > 0) gParts.push(' ')
          const a = rowAgents[j]
          const isSelected = gridFocused && rowIndices[j] === ss.gridIdx
          const isEntered = ss.enteredSession === a.id
          const borderColor = isEntered ? '#22c55e' : isSelected ? ACCENT : sessionBorderColor(a.status)
          const hint = isEntered ? ' Esc:exit ' : isSelected ? ' Enter:connect ' : ''
          const bar = '─'.repeat(Math.max(0, thisCellW - 2 - hint.length))
          gParts.push(t`${fg(borderColor)(`╰${bar}${hint}╯`)}`)
        }
      }
    }

    // gridText.content = joinStyled(...gParts)  // DISABLED
  }

  return { root, update }
}
