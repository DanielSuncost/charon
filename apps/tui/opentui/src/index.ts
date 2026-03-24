/**
 * Charon TUI — three-view terminal interface.
 *
 * F1=Chat, F2=Dashboard, F3=Sessions. Enter submits input.
 *
 * OpenTUI gotchas solved here:
 * 1. Factory functions (Text(), Box()) return VNode proxies. Setting .content
 *    on them does nothing. Use instantiate() for dynamic renderables.
 * 2. StyledText can't be interpolated into t`` templates (becomes [object Object]).
 *    Concatenate chunk arrays manually with new StyledText([...a.chunks, ...b.chunks]).
 * 3. renderer.keyInput.on('keypress') works for global key handling.
 * 4. Call input.focus() after adding to tree for initial focus.
 */

import {
  Box, Input, Text, ScrollBox, createCliRenderer, instantiate,
  MarkdownRenderable, SyntaxStyle,
  t, fg, bg, bold, italic, dim, green, cyan, red, yellow,
  type StyledText, StyledText as SC,
  type TextChunk,
} from '@opentui/core'
import { renderMascot } from './mascot'
import { Backend, type BackendEvent } from './backend'
import { createDashboardLayout, createDashboardState, filteredAgents, type DashboardState } from './dashboard'
import { createSessionsLayout, createSessionsState, syncVisible, gridAgents as getGridAgents, type SessionsState } from './sessions'

// ============================================================================
// Types & State
// ============================================================================

type ViewName = 'chat' | 'dashboard' | 'sessions'
interface Agent { id: string; name: string; status: string; role: string; goal: string; project: string; mode: string }
interface Project { name: string; path: string; agents: string[] }
interface Session { id: string; agentId: string; agentName: string; status: string; project: string; location: string }

interface MenuItem { cmd: string; desc: string }

const MENU_ITEMS: MenuItem[] = [
  { cmd: '/setup provider lmstudio', desc: 'Use local LM Studio' },
  { cmd: '/setup provider claude-code', desc: 'Anthropic Claude (OAuth)' },
  { cmd: '/setup provider claude-code --force', desc: 'Claude OAuth (fresh login)' },
  { cmd: '/setup provider codex', desc: 'OpenAI Codex (OAuth)' },
  { cmd: '/setup provider api', desc: 'Custom API endpoint' },
  { cmd: '/setup model', desc: 'Choose model (shows picker)' },
  { cmd: '/setup api-key <key>', desc: 'Set API key directly' },
  { cmd: '/setup project <path>', desc: 'Set project directory' },
  { cmd: '/setup auth-code <CODE>', desc: 'Paste OAuth authorization code' },
  { cmd: '/setup complete', desc: 'Finish setup' },
  { cmd: '/setup status', desc: 'Show current config' },
  { cmd: '/setup reset', desc: 'Reset all config' },
  { cmd: '/setup no-provider', desc: 'Heuristic mode (no LLM)' },
  { cmd: '/provider', desc: 'Switch provider' },
  { cmd: '/model', desc: 'Switch model' },
  { cmd: '/resume', desc: 'Resume a previous session' },
  { cmd: '/reset', desc: 'Clear conversation' },
  { cmd: '/dashboard', desc: 'Open dashboard (F2)' },
  { cmd: '/sessions', desc: 'Open sessions (F3)' },
  { cmd: '/agent create', desc: 'Create a new agent' },
  { cmd: '/hotkeys', desc: 'Show all keyboard shortcuts' },
  { cmd: '/timestamps', desc: 'Toggle message timestamps' },
  { cmd: '/help', desc: 'Show this menu' },
]

const S = {
  view: 'chat' as ViewName,
  msgs: [] as { styled: StyledText; ts: number }[],
  streaming: false,
  streamStartTs: 0,
  buf: [] as string[],
  ob: { complete: false, provider: '', model: '', step: '', project: '' },
  agentMode: 'interactive' as string,
  batchProgress: '' as string,
  tokensIn: 0,
  tokensOut: 0,
  contextPct: 0,
  thinkingLevel: 'medium',
  agents: [] as Agent[],
  projects: [] as Project[],
  sessions: [] as Session[],
  activity: [] as string[],
  dashIdx: 0,
  dashSection: 'agents' as 'agents' | 'projects',
  projIdx: 0,
  sessIdx: 0,
  // Info pane (Ctrl+I)
  infoPaneOpen: true,
  infoPaneTab: 0,  // 0=Tasks, 1=Goals, 2=User Model
  sessionInfo: null as any,
  // Slash menu
  menuOpen: false,
  menuIdx: 0,
  menuItems: [] as MenuItem[],
  menuFilter: '',
  // Timestamp toggle (Ctrl+T)
  showTimestamps: false,
  // Heartbeat
  lastHeartbeatTs: 0,
  // Background process indicators (flash when active)
  lastConsolidationTs: 0,
  lastAutoTaskTs: 0,
}

function fmtTs(epoch: number): string {
  if (!epoch) return ''
  const d = new Date(epoch)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

// addStatusRenderable is set after chat scroll is created
let _addStatusFn: ((s: StyledText) => void) | null = null

function pushMsg(styled: StyledText) {
  S.msgs.push({ styled, ts: Date.now() })
  if (_addStatusFn) _addStatusFn(styled)
}

const ic = (s: string) => s === 'running' ? '●' : s === 'idle' ? '○' : s === 'stopped' ? '✖' : '·'
const sc = (s: string) => s === 'running' ? '#22c55e' : s === 'stopped' ? '#ef4444' : '#6b7280'

// Helper: join StyledText objects by concatenating their chunk arrays.
// The t`` template can't interpolate StyledText (it becomes [object Object]).
function joinStyled(...parts: (StyledText | string)[]): StyledText {
  const chunks: TextChunk[] = []
  for (const p of parts) {
    if (typeof p === 'string') {
      chunks.push({ __isChunk: true, text: p } as TextChunk)
    } else if (p && (p as any).chunks) {
      chunks.push(...(p as any).chunks)
    }
  }
  return new SC(chunks)
}

function nl(): TextChunk { return { __isChunk: true, text: '\n' } as TextChunk }

// Charon's robe colors
const ROBE_BG = '#2a1215'      // warm dark red-brown (visible, not black)
const ROBE_BG_USER = '#0d1117' // dark blue-gray for user messages
const MANILA = '#e8d5a3'        // light manila/parchment for Charon's name

/** Format a Charon response with robe-red background, manila name, and basic markdown */
function charonMsg(text: string): StyledText {
  const w = Math.max(40, (process.stdout?.columns || 80) - 4)
  const parts: (StyledText | string)[] = []
  // Render markdown content with robe background, indented 1 char
  const rendered = renderSimpleMarkdown(text, w - 1)
  for (const line of rendered) {
    parts.push(t`${bg(ROBE_BG)(' ')}`)  // 1 char indent
    parts.push(line)
    parts.push('\n')
  }
  return joinStyled(...parts)
}

/** Parse inline markdown into segments with style info */
function parseInlineMarkdown(text: string): Array<{text: string, style: 'normal' | 'bold' | 'code' | 'italic'}> {
  const segments: Array<{text: string, style: 'normal' | 'bold' | 'code' | 'italic'}> = []
  // Use regex to find all inline markers
  const regex = /(\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g
  let lastIdx = 0
  let match

  while ((match = regex.exec(text)) !== null) {
    // Text before the match
    if (match.index > lastIdx) {
      segments.push({ text: text.slice(lastIdx, match.index), style: 'normal' })
    }

    if (match[2]) {
      // ***bold italic*** — render as bold
      segments.push({ text: match[2], style: 'bold' })
    } else if (match[3]) {
      // **bold**
      segments.push({ text: match[3], style: 'bold' })
    } else if (match[4]) {
      // *italic*
      segments.push({ text: match[4], style: 'italic' })
    } else if (match[5]) {
      // `code`
      segments.push({ text: match[5], style: 'code' })
    }

    lastIdx = match.index + match[0].length
  }

  // Remaining text after last match
  if (lastIdx < text.length) {
    segments.push({ text: text.slice(lastIdx), style: 'normal' })
  }

  // If no segments were found, return the whole text as normal
  if (segments.length === 0) {
    segments.push({ text, style: 'normal' })
  }

  return segments
}

/** Simple markdown renderer that produces StyledText lines with background color */
function renderSimpleMarkdown(text: string, w: number): StyledText[] {
  const lines = text.split('\n')
  const result: StyledText[] = []
  let inCodeBlock = false
  let codeLang = ''

  for (const rawLine of lines) {
    const line = rawLine

    // Code block toggle
    if (line.trimStart().startsWith('```')) {
      inCodeBlock = !inCodeBlock
      if (inCodeBlock) {
        codeLang = line.trim().slice(3).trim()
        const label = codeLang ? ` ${codeLang} ` : ''
        const codePad = '─'.repeat(Math.max(0, w - label.length - 3))
        result.push(t`${fg('#6b7280')(bg('#161b22')(` ┌${label}${codePad}`))}`)
      } else {
        const codePad = '─'.repeat(Math.max(0, w - 3))
        result.push(t`${fg('#6b7280')(bg('#161b22')(` └${codePad}`))}`)
        codeLang = ''
      }
      continue
    }

    if (inCodeBlock) {
      // Code content — monospace look with darker background
      const padded = '  ' + line + ' '.repeat(Math.max(0, w - line.length - 2))
      result.push(t`${fg('#e6edf3')(bg('#161b22')(padded.slice(0, w)))}`)
      continue
    }

    // Headers
    if (line.startsWith('### ')) {
      const content = ' ' + line.slice(4)
      const padded = content + ' '.repeat(Math.max(0, w - content.length))
      result.push(t`${bold(fg('#e8d5a3')(bg(ROBE_BG)(padded.slice(0, w))))}`)
      continue
    }
    if (line.startsWith('## ')) {
      const content = ' ' + line.slice(3)
      const padded = content + ' '.repeat(Math.max(0, w - content.length))
      result.push(t`${bold(fg('#e8d5a3')(bg(ROBE_BG)(padded.slice(0, w))))}`)
      continue
    }
    if (line.startsWith('# ')) {
      const content = ' ' + line.slice(2)
      const padded = content + ' '.repeat(Math.max(0, w - content.length))
      result.push(t`${bold(fg('#f0e6d0')(bg(ROBE_BG)(padded.slice(0, w))))}`)
      continue
    }

    // Bullet points — convert marker, then fall through to inline parser
    // Use negative lookahead to avoid matching **bold** at start of line
    let processedLine = line
    if (line.match(/^\s*-\s/) || line.match(/^\s*\*\s(?!\*)/)) {
      processedLine = line.replace(/^(\s*)[-*]\s/, '$1• ')
    }

    // All remaining text (bullets, numbered lists, regular) — parse inline markdown
    const segments = parseInlineMarkdown(' ' + processedLine)
    const segChunks: TextChunk[] = []
    let segLen = 0
    for (const seg of segments) {
      segLen += seg.text.length
      if (seg.style === 'bold') {
        segChunks.push(bold(fg('#f0e6d0')(bg(ROBE_BG)(seg.text))))
      } else if (seg.style === 'italic') {
        segChunks.push(italic(fg('#d4c4a8')(bg(ROBE_BG)(seg.text))))
      } else if (seg.style === 'code') {
        segChunks.push(fg('#7dd3fc')(bg('#1a1a2e')(` ${seg.text} `)))
        segLen += 2
      } else {
        segChunks.push(fg('#e0d0c0')(bg(ROBE_BG)(seg.text)))
      }
    }
    const remaining = Math.max(0, w - segLen)
    if (remaining > 0) segChunks.push(fg('#e0d0c0')(bg(ROBE_BG)(' '.repeat(remaining))))
    result.push(new SC(segChunks))
  }

  return result
}

/** Format a user message — no label, cool grey background */
function userMsg(text: string): StyledText {
  const w = Math.max(40, (process.stdout?.columns || 80) - 4)
  const parts: (StyledText | string)[] = []
  const lines = text.split('\n')
  for (const line of lines) {
    const padded = ' ' + line + ' '.repeat(Math.max(0, w - line.length - 1))
    parts.push(t`${fg('#e2e8f0')(bg('#1e2433')(padded))}`)
    parts.push('\n')
  }
  return joinStyled(...parts)
}

/** Format streaming charon response */
function charonStreamMsg(text: string): StyledText {
  const w = Math.max(40, (process.stdout?.columns || 80) - 4)
  const parts: (StyledText | string)[] = []
  // Render with markdown, indented 1 char
  const rendered = renderSimpleMarkdown(text, w - 1)
  for (const line of rendered) {
    parts.push(t`${bg(ROBE_BG)(' ')}`)
    parts.push(line)
    parts.push('\n')
  }
  // Cursor
  const cursorLine = '  ▊' + ' '.repeat(Math.max(0, w - 3))
  parts.push(t`${fg('#e0d0c0')(bg(ROBE_BG)(cursorLine))}`)
  return joinStyled(...parts)
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  const renderer = await createCliRenderer({ exitOnCtrlC: false, useMouse: false })
  _renderer = renderer

  // No mouse tracking at all. This gives us:
  // - Native right-click context menu (copy, paste, etc.)
  // - Native text selection (click-drag)
  // - Scroll wheel: gnome-terminal/VTE converts scroll to arrow keys in alt-screen
  //   mode when mouse tracking is off (alternateScroll). We catch those in the
  //   keypress handler below.
  const backend = new Backend()

  // ── Real renderable instances ──────────────────────────────────────────
  const mainText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const statusBar = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const statusBar2 = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const input = instantiate(renderer, Input({
    placeholder: 'Type /setup provider <name> to get started...',
    width: '100%', backgroundColor: '#0f172a', textColor: '#f8fafc',
  })) as any

  // ── Mascot ─────────────────────────────────────────────────────────────
  let mascotStyled = renderMascot(renderer.terminalWidth, renderer.terminalHeight).styled

  process.stdout.on('resize', () => {
    setTimeout(() => {
      const w = renderer.terminalWidth || process.stdout.columns || 80
      const h = renderer.terminalHeight || process.stdout.rows || 24
      mascotStyled = renderMascot(w, h).styled
      rebuildView()
      updateStatus()
    }, 100)
  })

  // ── Chat scroll container (must be created before functions that reference it) ──
  const chatScroll = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%', stickyScroll: true, stickyStart: 'bottom' })) as any

  // ── Chat view — per-message renderables ─────────────────────────────
  // Each message is its own renderable added to chatScroll.
  // This allows MarkdownRenderable for charon responses with proper
  // syntax highlighting, concealment, and streaming support.

  // Custom syntax style with markdown emphasis support
  const syntaxStyle = SyntaxStyle.fromTheme([
    { scope: ['markup.strong'], style: { bold: true, foreground: '#f0e6d0' } },
    { scope: ['markup.italic'], style: { italic: true, foreground: '#d4c4a8' } },
    { scope: ['markup.raw'], style: { foreground: '#7dd3fc' } },
    { scope: ['markup.strikethrough'], style: { dim: true } },
    { scope: ['markup.link'], style: { foreground: '#60a5fa', underline: true } },
    { scope: ['markup.link.url'], style: { foreground: '#60a5fa' } },
    { scope: ['markup.link.label'], style: { foreground: '#93c5fd' } },
    { scope: ['markup.heading'], style: { bold: true, foreground: '#e8d5a3' } },
    { scope: ['markup.list.marker'], style: { foreground: '#a78bfa' } },
    // Code highlighting
    { scope: ['keyword'], style: { foreground: '#c084fc' } },
    { scope: ['string'], style: { foreground: '#86efac' } },
    { scope: ['comment'], style: { foreground: '#6b7280', italic: true } },
    { scope: ['variable'], style: { foreground: '#93c5fd' } },
    { scope: ['function'], style: { foreground: '#fbbf24' } },
    { scope: ['number'], style: { foreground: '#f59e0b' } },
    { scope: ['operator'], style: { foreground: '#e2e8f0' } },
    { scope: ['type'], style: { foreground: '#67e8f9' } },
    { scope: ['constant'], style: { foreground: '#f59e0b' } },
    { scope: ['punctuation'], style: { foreground: '#9ca3af' } },
  ])
  const chatMsgRenderables: any[] = []  // track added renderables for cleanup

  // Mascot at the top of chat
  const mascotRenderable = instantiate(renderer, Text({ content: mascotStyled, width: '100%' })) as any

  // Welcome text (shown when no messages)
  const welcomeText = instantiate(renderer, Text({ content: t`${dim('\n  Welcome to Charon. Type a message to begin.')}`, width: '100%' })) as any

  // Streaming markdown renderable (reused during streaming)
  let streamingMd: any = null

  // Bottom spacing handled by chatScroll paddingBottom

  function clearChatRenderables() {
    for (const r of chatMsgRenderables) {
      try { chatScroll.remove(r.id) } catch {}
    }
    chatMsgRenderables.length = 0
    if (streamingMd) {
      try { chatScroll.remove(streamingMd.id) } catch {}
      streamingMd = null
    }
  }

  // Bottom spacer keeps last message visible above the fixed bottom bar
  const _bottomSpacer = instantiate(renderer, Box({ height: 8, width: '100%' })) as any
  chatScroll.add(_bottomSpacer)

  function scrollToBottom() {
    try { chatScroll.scrollTo(999999) } catch {}
  }

  function addChatRenderable(renderable: any) {
    try { chatScroll.insertBefore(renderable, _bottomSpacer) } catch { chatScroll.add(renderable) }
    chatMsgRenderables.push(renderable)
    scrollToBottom()
  }

  function addUserMessage(text: string) {
    ;(globalThis as any).__charonMsgCount = ((globalThis as any).__charonMsgCount || 0) + 1
    // Spacer between messages
    const spacer = instantiate(renderer, Box({ height: 1, width: '100%' })) as any
    addChatRenderable(spacer)

    // User message with cool grey background
    const w = Math.max(40, (process.stdout?.columns || 80) - 4)
    const lines = text.split('\n')
    const parts: (StyledText | string)[] = []
    for (const line of lines) {
      const padded = ' ' + line + ' '.repeat(Math.max(0, w - line.length - 1))
      parts.push(t`${fg('#e2e8f0')(bg('#1e2433')(padded))}`)
      parts.push('\n')
    }
    const userRenderable = instantiate(renderer, Text({ content: joinStyled(...parts), width: '100%' })) as any
    addChatRenderable(userRenderable)
  }

  function addCharonMessage(text: string) {
    // Spacer
    const spacer = instantiate(renderer, Box({ height: 1, width: '100%' })) as any
    addChatRenderable(spacer)

    // Use MarkdownRenderable for proper formatting
    const md = new MarkdownRenderable(renderer, {
      content: text,
      syntaxStyle: syntaxStyle,
      conceal: true,
      concealCode: false,
      streaming: false,
      selectable: true,
      width: '100%',
      paddingLeft: 1,
      backgroundColor: ROBE_BG,
    })
    addChatRenderable(md)
  }

  function addToolBlock(styledContent: StyledText) {
    const renderable = instantiate(renderer, Text({ content: styledContent, width: '100%' })) as any
    addChatRenderable(renderable)
  }

  function addStatusMessage(styledContent: StyledText) {
    const renderable = instantiate(renderer, Text({ content: styledContent, width: '100%' })) as any
    addChatRenderable(renderable)
  }

  function startStreaming() {
    // Create a streaming markdown renderable
    const spacer = instantiate(renderer, Box({ height: 1, width: '100%' })) as any
    addChatRenderable(spacer)

    streamingMd = new MarkdownRenderable(renderer, {
      content: '',
      syntaxStyle: syntaxStyle,
      conceal: true,
      concealCode: false,
      streaming: true,
      selectable: true,
      width: '100%',
      paddingLeft: 1,
      backgroundColor: ROBE_BG,
    })
    try { chatScroll.insertBefore(streamingMd, _bottomSpacer) } catch { chatScroll.add(streamingMd) }
  }

  function updateStreaming(text: string) {
    if (streamingMd) {
      streamingMd.content = text
      scrollToBottom()
    }
  }

  function finishStreaming() {
    if (streamingMd) {
      // Finalize: set streaming false, re-set content to force final render
      const finalContent = streamingMd.content
      streamingMd.streaming = false
      streamingMd.content = typeof finalContent === 'string' ? finalContent : S.buf.join('')
      chatMsgRenderables.push(streamingMd)
      streamingMd = null
      renderer.requestRender()
      scrollToBottom()
    }
  }

  // Initialize chat scroll with mascot + welcome + spacer
  try { chatScroll.insertBefore(mascotRenderable, _bottomSpacer) } catch { chatScroll.add(mascotRenderable) }
  try { chatScroll.insertBefore(welcomeText, _bottomSpacer) } catch { chatScroll.add(welcomeText) }

  // Wire up pushMsg to add renderables
  _addStatusFn = (styled: StyledText) => {
    addStatusMessage(styled)
    renderer.requestRender()
  }

  // ── Activity indicator (rowing animation, fixed above input bar) ─────
  function startRowingAnimation(tc?: {fg: string, bg: string}) {
    stopRowingAnimation()
    activityBox.height = 3
    activityBox.maxHeight = undefined
    activityBox.overflow = undefined

    // Style E/I inspired — red tones, big flickering lantern, shimmer dots
    const water = '#6366f1'    // indigo water
    const waveD = '#4338ca'    // darker wave
    const hull = '#7f1d1d'     // dark red hull
    const hullL = '#991b1b'    // lighter hull accent
    const figure = '#dc2626'   // red figure
    const figureD = '#991b1b'  // dark red
    const oar = '#d4c4a8'     // tan oar
    const lanternBright = '#fbbf24'  // bright yellow
    const lanternDim = '#f59e0b'     // orange
    const lanternGlow = '#fde68a'    // pale yellow glow
    const spark = '#fcd34d'    // spark yellow

    const frames = [
      // Frame 1: oar forward, lantern bright
      () => joinStyled(
        t`${fg(spark)('  .*')}`, t`${fg(lanternBright)('◈')}`, t`${fg(spark)('·.          ')}`, '\n',
        t`${fg(water)('  ≈ ')}`, t`${fg(figure)('⟨')}`, t`${fg(figureD)('█')}`, t`${fg(figure)('⟩')}`, t`${fg(oar)(' ╱')}`, t`${fg(water)('   ≈     ')}`, '\n',
        t`${fg(water)('  ≈')}`, t`${fg(waveD)('~')}`, t`${fg(hull)('╘')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('▬')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('╛')}`, t`${fg(waveD)('~')}`, t`${fg(water)('≈')}`, t`${fg(waveD)('~~  ')}`,
      ),
      // Frame 2: oar vertical, lantern orange
      () => joinStyled(
        t`${fg(spark)('  .·')}`, t`${fg(lanternDim)('◈')}`, t`${fg(spark)('*.          ')}`, '\n',
        t`${fg(water)('  ≈ ')}`, t`${fg(figure)('⟨')}`, t`${fg(figureD)('█')}`, t`${fg(figure)('⟩')}`, t`${fg(oar)(' │')}`, t`${fg(water)('   ≈     ')}`, '\n',
        t`${fg(waveD)(' ~')}`, t`${fg(water)('≈')}`, t`${fg(hull)('╘')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('▬')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('╛')}`, t`${fg(water)('≈')}`, t`${fg(waveD)('~')}`, t`${fg(water)('≈   ')}`,
      ),
      // Frame 3: oar back, lantern bright
      () => joinStyled(
        t`${fg(spark)('   *')}`, t`${fg(lanternBright)('◈')}`, t`${fg(lanternGlow)('˙')}`, t`${fg(spark)('         ')}`, '\n',
        t`${fg(water)('  ≈ ')}`, t`${fg(figure)('⟨')}`, t`${fg(figureD)('█')}`, t`${fg(figure)('⟩')}`, t`${fg(oar)('  ╲')}`, t`${fg(water)('  ≈     ')}`, '\n',
        t`${fg(water)(' ≈')}`, t`${fg(waveD)('~')}`, t`${fg(water)('≈')}`, t`${fg(hull)('╘')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('▬')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('╛')}`, t`${fg(waveD)('~')}`, t`${fg(water)('≈   ')}`,
      ),
      // Frame 4: oar vertical, lantern dim
      () => joinStyled(
        t`${fg(spark)('  ·')}`, t`${fg(lanternDim)('◈')}`, t`${fg(spark)(' *.         ')}`, '\n',
        t`${fg(water)('  ≈ ')}`, t`${fg(figure)('⟨')}`, t`${fg(figureD)('█')}`, t`${fg(figure)('⟩')}`, t`${fg(oar)(' │')}`, t`${fg(water)('   ≈     ')}`, '\n',
        t`${fg(waveD)('  ~')}`, t`${fg(water)('≈')}`, t`${fg(hull)('╘')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('▬')}`, t`${fg(hullL)('▬▬')}`, t`${fg(hull)('╛')}`, t`${fg(water)('≈')}`, t`${fg(waveD)('~~  ')}`,
      ),
    ]

    let frame = 0
    activityText.content = frames[0]()
    if (!(S as any)._rowId) (S as any)._rowId = 0
    ;(S as any)._rowId += 1
    const rowId = (S as any)._rowId
    ;(S as any)._rowInterval = setInterval(() => {
      if ((S as any)._rowId !== rowId) { clearInterval((S as any)._rowInterval); return }
      frame = (frame + 1) % frames.length
      activityText.content = frames[frame]()
      renderer.requestRender()
    }, 300)
  }

  function stopRowingAnimation() {
    if ((S as any)._rowInterval) {
      clearInterval((S as any)._rowInterval)
      ;(S as any)._rowInterval = null
    }
    activityText.content = ''
    activityBox.height = 0
    activityBox.maxHeight = 0
    activityBox.overflow = 'hidden'
    renderer.requestRender()
  }

  // Legacy buildChat — no-op, chat is built incrementally
  function buildChat(): StyledText {
    return t`${''}`
  }

  // ── Dashboard (real multi-column layout) ────────────────────────────
  const DS = createDashboardState()
  const dashboard = createDashboardLayout(renderer)
  const SS = createSessionsState()
  const sessions = createSessionsLayout(renderer)

  // Build sessions grid using SAME Box pattern as dashboard (works in tmux)
  const sessGrid = (() => {
    const BORDER = '#3b3347'
    const ACCENT = '#a78bfa'

    const sidebarText = instantiate(renderer, Text({ content: '', width: '100%' })) as any

    function cell(text: any, widthPct: string | undefined, grow?: boolean) {
      const scrollInner = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%', stickyScroll: true, stickyStart: 'bottom' })) as any
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

    // Pre-create grid cells (4 rows × 3 cols = 12 cells max)
    const MAX_CELLS = 12
    const MAX_COLS = 3
    const MAX_ROWS = Math.ceil(MAX_CELLS / MAX_COLS)
    const gridTexts: any[] = []
    const gridCells: any[] = []
    const gridScrolls: any[] = []
    for (let i = 0; i < MAX_CELLS; i++) {
      const txt = instantiate(renderer, Text({ content: '', width: '100%' })) as any
      gridTexts.push(txt)
      const c = cell(txt, undefined, true)
      gridCells.push(c)
      // The cell function creates: Box > ScrollBox > Text
      // Get the ScrollBox reference for scrolling
      try { gridScrolls.push(c.getChildren()[0]) } catch { gridScrolls.push(null) }
    }

    // Layout: sidebar (left, full height) + grid area (right, rows of cells)
    const sidebarCell = cell(sidebarText, '22%')
    
    // Grid area: column of rows, each row has MAX_COLS cells
    const gridArea = instantiate(renderer, Box({
      flexGrow: 1, flexDirection: 'column',
    })) as any
    for (let r = 0; r < MAX_ROWS; r++) {
      const row = instantiate(renderer, Box({
        flexGrow: 1, width: '100%', flexDirection: 'row',
      })) as any
      for (let c = 0; c < MAX_COLS; c++) {
        row.add(gridCells[r * MAX_COLS + c])
      }
      gridArea.add(row)
    }

    // Top-level: row with sidebar + grid area
    const root = instantiate(renderer, Box({
      flexGrow: 1, width: '100%', height: '100%', flexDirection: 'row',
    })) as any
    root.add(sidebarCell)
    root.add(gridArea)

    function renderAgent(a: any, selected: boolean, entered: boolean): StyledText {
      const p: (StyledText | string)[] = []
      if (!a) return joinStyled(t`${dim('(empty)')}`)

      // ── Header: agent info in bold blue ──
      const nameColor = entered ? '#22c55e' : selected ? '#c4b5fd' : '#60a5fa'
      const icon = entered ? '⏺' : a.status === 'running' ? '●' : '○'
      const displayName = a.liveSessionId 
        ? `${a.name} (${a.liveSessionId.split('-').pop()?.slice(0, 6) || ''})`
        : a.name
      const project = (a.project || '').split('/').pop() || ''
      const goal = (a.goal || a.last_summary || '').slice(0, 40)
      const headerParts = [displayName, a.role, project].filter(Boolean).join(' · ')
      p.push(t`${bold(fg(nameColor)(`${icon} ${headerParts}`))}`)
      if (entered) p.push(t`${fg('#22c55e')(' ⏺')}`)
      else if (selected) p.push(t`${fg('#7c3aed')(' ◄')}`)
      if (goal) { p.push('\n'); p.push(t`${dim(fg('#7c9fc4')(goal))}`) }
      p.push('\n')

      // ── Content: conversation or tmux capture ──
      const tmuxContent = SS.tmuxContent.get(a.id) || ''
      const sid = a.liveSessionId || a.id?.replace('live-', '') || ''
      const convContent = (S as any)._convCache?.get(sid) || ''

      if (tmuxContent) {
        // Tmux capture — filter chrome, show content
        const lines = tmuxContent.split('\n')
          .map((l: string) => l.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').trim())
          .filter((l: string) => {
            if (!l) return false
            if (/^[╭╰╮╯│├┤─═\s]+$/.test(l)) return false
            if (/Type a message|F[123]:|Ctrl\+|AG-\d+|interactive|ctx:|effort:|provider/i.test(l)) return false
            return (l.match(/[a-zA-Z]{3,}/g) || []).length > 0
          })
        for (const l of lines.slice(-20)) { p.push(t`${dim(l)}`); p.push('\n') }
      } else if (convContent) {
        // Live conversation from JSONL — formatted like chat
        for (const l of convContent.split('\n').slice(-25)) {
          if (l.startsWith('❯')) {
            // User message
            p.push(t`${fg('#9ca3af')(l)}`)
          } else if (l.match(/^\s*\[(?:Read|Write|Edit|Bash|Http|Git)/)) {
            // Tool call
            p.push(t`${fg('#93c5fd')(l)}`)
          } else if (l.match(/^\s*⚡|^\s*📄|^\s*✏️|^\s*🔧/)) {
            // Tool icon
            p.push(t`${fg('#93c5fd')(l)}`)
          } else if (l.trim() === '') {
            p.push('')
          } else {
            // Assistant message
            p.push(t`${fg('#d4c4a8')(l)}`)
          }
          p.push('\n')
        }
      } else if (a.hasTmux) {
        p.push(t`${dim('◐ Loading...')}`)
      } else if (a.isLive || a.source === 'live') {
        p.push(t`${dim('◐ Waiting for messages...')}`)
      } else {
        p.push(t`${dim('No connection')}`)
        p.push('\n')
        p.push(t`${dim('charons-boat wrap -- ' + (a.name || 'agent'))}`)
      }

      return joinStyled(...p)
    }

    function update() {
      const allAgents = (S.agents as any[]).filter(a =>
        a.role !== 'shade' && a.status !== 'stopped'
      )
      // Default visibility: charon agents + live sessions + charons-boat agents
      // Non-charon agents without boat are hidden by default (user can toggle)
      if (SS.visible.size === 0) {
        for (const a of allAgents) {
          if (a.role === 'charon' || a.source === 'charon' || a.source === 'live' || a.isLive || (a as any).hasBoat) {
            SS.visible.add(a.id)
          }
        }
      } else {
        // Auto-add new live sessions
        for (const a of allAgents) {
          if ((a.isLive || a.source === 'live') && !SS.visible.has(a.id)) {
            SS.visible.add(a.id)
          }
        }
      }
      const visibleAgents = allAgents.filter(a => SS.visible.has(a.id))
      const inAgents = SS.section === 'agents'

      // Sidebar
      const sp: (StyledText | string)[] = []
      sp.push(t`${bold(fg(ACCENT)(inAgents ? '▸ Agents' : '  Agents'))}`)
      sp.push('\n')
      sp.push(t`${dim('Enter: toggle  Tab: switch')}`)
      for (let i = 0; i < allAgents.length; i++) {
        const a = allAgents[i]
        const sel = inAgents && i === SS.agentIdx
        const checked = SS.visible.has(a.id)
        const icon = a.status === 'running' ? '●' : '○'
        const srcIcon = a.hasTmux ? '⬡' : a.isLive || a.source === 'live' ? '◈' : '·'
        const shortName = a.liveSessionId 
          ? `${a.name.split(' ')[0]}·${a.liveSessionId.split('-').pop()?.slice(0, 4) || ''}`
          : a.name
        sp.push('\n')
        sp.push(sel
          ? t`${bold(fg('#c4b5fd')(`▸ ${checked ? '[✓]' : '[ ]'} ${icon}${srcIcon} ${shortName}`))}`
          : t`${fg('#9ca3af')(`  ${checked ? '[✓]' : '[ ]'} ${icon}${srcIcon} ${shortName}`)}`
        )
      }
      sidebarText.content = joinStyled(...sp)

      // Fill grid cells with visible agents
      const inGrid = SS.section === 'grid'
      for (let i = 0; i < MAX_CELLS; i++) {
        if (i < visibleAgents.length) {
          const a = visibleAgents[i]
          const selected = inGrid && i === SS.gridIdx
          const entered = SS.enteredSession === a.id
          let cellContent = renderAgent(a, selected, entered)
          // Add inline input for entered cell
          if (entered) {
            const currentInput = (S as any)._steerInput || ''
            cellContent = joinStyled(cellContent, '\n', t`${fg('#22c55e')(`❯ ${currentInput}█`)}`)
          }
          gridTexts[i].content = cellContent
        } else {
          gridTexts[i].content = ''
        }
      }
    }

    return { root, update, gridScrolls }
  })()

  function buildDashboard(): StyledText {
    // This is a fallback — the real dashboard uses its own Box tree
    // This only runs if we somehow end up in text-mode dashboard
    // Matches the curses draw_dashboard_mode layout:
    // - Reverse-video header bar
    // - Left half: System stats, Agents (navigable), Current Goal, Projects (navigable)
    // - Right half: Rear-view mirror (recent activity)
    // The two halves are rendered line by line, padded to terminal width.

    const w = process.stdout.columns || 80
    const midCol = Math.max(45, Math.floor(w / 2))
    const agents = S.agents.filter(a => a.role !== 'shade')
    const pad = (s: string, len: number) => s.length >= len ? s.slice(0, len) : s + ' '.repeat(len - s.length)

    // Build left-side lines and right-side lines, then merge
    const leftLines: (StyledText | string)[] = []
    const rightLines: (StyledText | string)[] = []

    // ── Left side ──────────────────────────────────────────────

    // System stats
    const pending = S.agents.filter(a => a.status === 'running').length
    const stopped = S.agents.filter(a => a.status === 'stopped').length
    leftLines.push(t`${bold('  System')}`)
    leftLines.push(t`${dim(`    Agents: ${agents.length} active, ${stopped} stopped`)}`)
    leftLines.push(t`${dim(`    Provider: ${S.ob.provider || 'none'}  Model: ${S.ob.model || 'none'}`)}`)
    leftLines.push(t`${dim(`    Setup: ${S.ob.complete ? 'complete' : S.ob.step}`)}`)
    leftLines.push('')

    // Agents list
    const agentHeader = S.dashSection === 'agents'
      ? t`${bold(fg('#a78bfa')('  ▸ Agents'))}` : t`${dim('  Agents')}`
    leftLines.push(agentHeader)

    if (agents.length === 0) {
      leftLines.push(t`${dim('    (none — /agent create to add)')}`)
    } else {
      for (let i = 0; i < Math.min(agents.length, 10); i++) {
        const a = agents[i]
        const sel = S.dashSection === 'agents' && i === S.dashIdx
        if (sel) {
          leftLines.push(joinStyled(
            t`${bold(fg('#a78bfa')('  ▶ '))}`,
            t`${fg(sc(a.status))(`${ic(a.status)} ${a.name}`)}`,
            t`${dim(` (${a.role})`)}`,
          ))
        } else {
          leftLines.push(joinStyled(
            t`${dim('    ')}`,
            t`${fg(sc(a.status))(`${ic(a.status)} ${a.name}`)}`,
            t`${dim(` (${a.role})`)}`,
          ))
        }
      }
    }
    leftLines.push('')

    // Selected agent detail
    const selAgent = (S.dashSection === 'agents' && agents[S.dashIdx]) ? agents[S.dashIdx] : null
    if (selAgent) {
      leftLines.push(t`${bold('  Agent Detail')}`)
      leftLines.push(joinStyled(t`${dim('    ID:      ')}`, t`${selAgent.id}`))
      leftLines.push(joinStyled(t`${dim('    Goal:    ')}`, t`${selAgent.goal || '—'}`))
      leftLines.push(joinStyled(t`${dim('    Project: ')}`, t`${(selAgent.project || '—').split('/').pop() || '—'}`))
      leftLines.push(joinStyled(t`${dim('    Status:  ')}`, t`${fg(sc(selAgent.status))(selAgent.status)}`))
    }
    leftLines.push('')

    // Current Goal
    leftLines.push(t`${bold('  Current Goal')}`)
    leftLines.push(t`${dim('    Build persistent long-horizon agent OS')}`)
    const pct = Math.min(100, Math.max(0, Math.floor((agents.length / 10) * 100)))
    const barW = Math.min(40, midCol - 10)
    const fill = Math.floor((pct / 100) * barW)
    leftLines.push(t`${dim('    [' + '█'.repeat(fill) + '░'.repeat(barW - fill) + '] ' + pct + '%')}`)
    leftLines.push('')

    // Projects list
    const projHeader = S.dashSection === 'projects'
      ? t`${bold(fg('#a78bfa')('  ▸ Projects'))}` : t`${dim('  Projects')}`
    leftLines.push(projHeader)

    if (S.projects.length === 0) {
      leftLines.push(t`${dim('    (none)')}`)
    } else {
      for (let i = 0; i < S.projects.length; i++) {
        const p = S.projects[i]
        const sel = S.dashSection === 'projects' && i === S.projIdx
        if (sel) {
          leftLines.push(joinStyled(
            t`${bold(fg('#a78bfa')('  ▶ '))}`,
            t`${bold(p.name)}`,
            t`${dim(` (${p.agents.length} agents)`)}`,
          ))
        } else {
          leftLines.push(joinStyled(t`${dim('    ')}`, t`${p.name}`, t`${dim(` (${p.agents.length})`)}` ))
        }
      }
    }
    leftLines.push('')
    leftLines.push(t`${dim('  Controls')}`)
    leftLines.push(t`${dim('    ↑↓: navigate  Tab: agents/projects  Enter: select')}`)
    leftLines.push(t`${dim('    F1: chat  F2: dashboard  F3: sessions')}`)

    // ── Right side (Rear-view mirror) ──────────────────────────

    rightLines.push(t`${bold('Rear-view mirror')}`)
    if (S.activity.length === 0) {
      rightLines.push(t`${dim('  (no recent activity)')}`)
    } else {
      for (const a of S.activity.slice(-15)) {
        rightLines.push(t`${dim(a.slice(0, w - midCol - 2))}`)
      }
    }

    // ── Merge left and right side by side ──────────────────────
    // We can't do true side-by-side in a single Text renderable,
    // so render left side fully, then right side below with a header.

    const parts: (StyledText | string)[] = []

    // Header (reverse-style)
    parts.push(t`${bold(fg('#0a0a12')(` Charon Dashboard  │  TAB:switch  F1:chat  F3:sessions `))}`)
    parts.push('\n')

    // Left content
    for (const l of leftLines) {
      parts.push('\n')
      parts.push(l)
    }

    // Divider
    parts.push('\n\n')
    parts.push(t`${fg('#3b3252')('  ' + '─'.repeat(Math.max(1, w - 4)))}`)

    // Right content (Rear-view mirror)
    parts.push('\n')
    for (const r of rightLines) {
      parts.push('\n')
      parts.push(joinStyled('  ', r))
    }

    return joinStyled(...parts)
  }

  function buildSessions(): StyledText {
    const parts: (StyledText | string)[] = []
    const termW = renderer.terminalWidth || 80
    const termH = renderer.terminalHeight || 24

    // All agents that could appear in the sidebar
    const allAgents = (S.agents as any[]).filter(a =>
      a.role !== 'shade' && a.status !== 'stopped'
    )
    
    // Visible in grid: only agents checked in the sidebar
    // Default: show agents with tmux, charon agents, or charons-boat connected
    if (SS.visible.size === 0 && allAgents.length > 0) {
      for (const a of allAgents) {
        if (a.hasTmux || a.role === 'charon' || a.source === 'charon' || (a as any).hasBoat) {
          SS.visible.add(a.id)
        }
      }
    }
    const visibleAgents = allAgents.filter(a => SS.visible.has(a.id))

    // Layout: left sidebar (agent list) + right grid (session cells)
    const sideW = Math.min(24, Math.max(16, Math.floor(termW * 0.2)))
    const gridW = termW - sideW - 3

    // Build sidebar lines with selection state
    const sideLines: { text: string, style: 'header' | 'selected' | 'normal' | 'dim' }[] = []
    const inAgents = SS.section === 'agents'
    const inProjects = SS.section === 'projects'
    
    sideLines.push({ text: inAgents ? '▸ Agents' : '  Agents', style: inAgents ? 'header' : 'dim' })
    sideLines.push({ text: 'Enter: toggle', style: 'dim' })
    sideLines.push({ text: '', style: 'normal' })
    
    for (let i = 0; i < allAgents.length; i++) {
      const a = allAgents[i]
      const sel = inAgents && i === SS.agentIdx
      const checked = SS.visible.has(a.id)
      const icon = a.status === 'running' ? '●' : '○'
      const check = checked ? '[✓]' : '[ ]'
      const prefix = sel ? '▸' : ' '
      sideLines.push({
        text: `${prefix} ${check} ${icon} ${a.name}`.slice(0, sideW - 1),
        style: sel ? 'selected' : 'normal',
      })
    }
    
    sideLines.push({ text: '', style: 'normal' })
    sideLines.push({ text: inProjects ? '▸ Projects' : '  Projects', style: inProjects ? 'header' : 'dim' })
    sideLines.push({ text: 'Enter: filter', style: 'dim' })
    sideLines.push({ text: '', style: 'normal' })
    
    const projectSet = new Set<string>()
    for (const a of allAgents) {
      const p = (a.project || '').split('/').pop() || ''
      if (p) projectSet.add(p)
    }
    const projList = ['All Projects', ...projectSet]
    for (let i = 0; i < projList.length; i++) {
      const sel = inProjects && i === SS.projectIdx
      const prefix = sel ? '▸ ' : '  '
      sideLines.push({
        text: `${prefix}${projList[i]}`,
        style: sel ? 'selected' : 'normal',
      })
    }

    // Build grid content
    if (visibleAgents.length === 0) {
      // No agents — just render sidebar + empty message
      for (let row = 0; row < termH - 4; row++) {
        const side = (sideLines[row] || '').slice(0, sideW).padEnd(sideW)
        const grid = row === 2 ? '  No active sessions.' : ''
        parts.push(joinStyled(t`${dim(side)}`, t`${dim(' │ ')}`, t`${dim(grid)}`))
        parts.push('\n')
      }
      return joinStyled(...parts)
    }

    // Grid cells
    const cols = Math.min(2, visibleAgents.length)
    const cellW = Math.floor((gridW - (cols - 1)) / cols)
    const gridRows = Math.ceil(visibleAgents.length / cols)
    const cellH = Math.max(6, Math.min(18, Math.floor((termH - 4) / gridRows) - 2))

    // Pre-build grid lines
    const gridLines: string[] = []
    gridLines.push(`Session Grid — ${visibleAgents.length} visible`)

    for (let r = 0; r < gridRows; r++) {
      const rowAgents = visibleAgents.slice(r * cols, r * cols + cols)
      
      // Header
      let header = ''
      for (let c = 0; c < rowAgents.length; c++) {
        const a = rowAgents[c]
        const icon = a.status === 'running' ? '●' : '○'
        const label = ` ${icon} ${a.name} `
        const bar = '─'.repeat(Math.max(0, cellW - label.length - 2))
        header += '╭─' + label + bar + '╮'
        if (c < rowAgents.length - 1) header += ' '
      }
      gridLines.push(header)

      // Content
      for (let row = 0; row < cellH; row++) {
        let line = ''
        for (let c = 0; c < rowAgents.length; c++) {
          const a = rowAgents[c]
          const tmuxContent = SS.tmuxContent.get(a.id) || ''
          const tmuxLines = tmuxContent.split('\n').filter((l: string) => {
            const clean = l.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').trim()
            if (!clean) return false
            if (/^[╭╰╮╯│├┤─═\s]+$/.test(clean)) return false
            if (/Type a message|F[123]:|Ctrl\+|AG-\d+|interactive|ctx:|effort:|provider/i.test(clean)) return false
            return (clean.match(/[a-zA-Z]{3,}/g) || []).length > 0
          })
          const visible = tmuxLines.slice(-cellH)
          let content = ''
          if (visible[row]) {
            content = visible[row].replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/^[\s│┃|╭╰╮╯├┤─═►▸●○\[\]✓]+/, '').slice(0, cellW - 2)
          } else if (!tmuxContent && row === 0) {
            content = ` ${a.status} · ${a.role}`
          } else if (!tmuxContent && row === 1) {
            content = ' (no tmux capture)'
          }
          content = content.padEnd(cellW - 2).slice(0, cellW - 2)
          line += '│' + content + '│'
          if (c < rowAgents.length - 1) line += ' '
        }
        gridLines.push(line)
      }

      // Footer
      let footer = ''
      for (let c = 0; c < rowAgents.length; c++) {
        footer += '╰' + '─'.repeat(cellW - 2) + '╯'
        if (c < rowAgents.length - 1) footer += ' '
      }
      gridLines.push(footer)
    }

    // Merge sidebar + grid line by line
    const maxLines = Math.max(sideLines.length, gridLines.length, termH - 4)
    for (let row = 0; row < maxLines; row++) {
      const sideEntry = sideLines[row]
      const sideText = (sideEntry?.text || '').slice(0, sideW).padEnd(sideW)
      const sideStyle = sideEntry?.style || 'normal'
      const grid = gridLines[row] || ''
      
      const styledSide = sideStyle === 'header' ? t`${bold(fg('#a78bfa')(sideText))}`
        : sideStyle === 'selected' ? t`${bold(fg('#c4b5fd')(sideText))}`
        : sideStyle === 'dim' ? t`${dim(sideText)}`
        : t`${fg('#9ca3af')(sideText)}`
      
      const styledGrid = row === 0 ? t`${bold(fg('#a78bfa')(grid))}`
        : grid.includes('╭') || grid.includes('╰') ? t`${fg('#7c3aed')(grid)}`
        : t`${dim(grid)}`
      
      parts.push(joinStyled(styledSide, t`${dim(' │ ')}`, styledGrid))
      parts.push('\n')
    }

    return joinStyled(...parts)
  }

  function rebuildView() {

    if (S.view === 'chat') {
      // Chat is rendered incrementally — just request render
      renderer.requestRender()
    } else if (S.view === 'dashboard') {
      // Update the real dashboard
      DS.agents = S.agents
      DS.projects = S.projects
      DS.activity = S.activity
      dashboard.update(DS)
    } else {
      // Sessions: update grid directly, then return to skip menu/requestRender below
      const liveState = (S as any)._liveState as Map<string, {status: string, summary: string}> | undefined
      SS.agents = (S.agents as any[]).map((a: any) => {
        const live = liveState?.get(a.id)
        if (live) return { ...a, status: live.status || a.status, last_summary: live.summary || a.last_summary }
        return a
      }) as any
      SS.projects = S.projects
      sessGrid.update()
      return  // Skip menu text update and requestRender — they cause tmux collapse
    }
    // Update menu overlay (rendered in bottom bar, not chat scroll)
    if (S.menuOpen && S.menuItems.length > 0 && S.view === 'chat') {
      const menuW = Math.min(72, Math.max(58, (process.stdout?.columns || 80) - 8))
      const border = fg('#7c3aed')
      const hasAge = S.menuItems.some((it: any) => it.age)
      const title = hasAge ? 'Sessions' : 'Commands'
      const mp: (StyledText | string)[] = []
      mp.push(t`${border(`  ╭─ ${title} ${'─'.repeat(menuW - title.length - 5)}╮`)}`)
      for (let i = 0; i < S.menuItems.length; i++) {
        const item = S.menuItems[i] as any
        const sel = i === S.menuIdx
        const age = item.age || ''
        mp.push('\n')
        if (hasAge) {
          // Resume-style: preview left, age right-aligned
          const prefix = sel ? ' ▸ ' : '   '
          const innerW = menuW - 6 // border + prefix + border
          const ageW = age.length
          const descW = Math.max(10, innerW - ageW - 2)
          const desc = item.desc.length > descW ? item.desc.slice(0, descW - 1) + '…' : item.desc
          const padded = desc + ' '.repeat(Math.max(0, descW - desc.length))
          if (sel) {
            mp.push(joinStyled(
              t`${border('  │')}`,
              t`${bold(fg('#c4b5fd')(prefix))}`,
              t`${bold(fg('#f8fafc')(padded))}`,
              t`${fg('#6b7280')(' ' + age)}`,
              t`${border(' │')}`,
            ))
          } else {
            mp.push(joinStyled(
              t`${border('  │')}`,
              t`${fg('#9ca3af')(prefix)}`,
              t`${fg('#9ca3af')(padded)}`,
              t`${dim(' ' + age)}`,
              t`${border(' │')}`,
            ))
          }
        } else {
          // Command-style: cmd + desc
          const cmdPad = item.cmd + ' '.repeat(Math.max(0, 30 - item.cmd.length))
          if (sel) {
            mp.push(joinStyled(
              t`${border('  │')}`,
              t`${bold(fg('#c4b5fd')(' ▸ '))}`,
              t`${bold(fg('#f8fafc')(cmdPad))}`,
              t`${fg('#c4b5fd')(item.desc)}`,
            ))
          } else {
            mp.push(joinStyled(
              t`${border('  │')}`,
              '   ',
              t`${fg('#9ca3af')(cmdPad)}`,
              t`${dim(item.desc)}`,
            ))
          }
        }
      }
      mp.push('\n')
      mp.push(t`${border(`  ╰─ ↑↓ navigate  Enter select  Esc close ${'─'.repeat(Math.max(0, menuW - 44))}╯`)}`)
      menuText.content = joinStyled(...mp)
    } else {
      menuText.content = ''
    }
    if (S.view !== 'sessions') renderer.requestRender()
  }

  function updateStatus() {
    if (S.view === 'sessions') {
      statusBar.content = t`${dim('  Tab:switch  ↑↓←→:navigate  Enter:connect  Esc:back')}`
      statusBar2.content = ''
      return
    }

    const termW = process.stdout.columns || 80

    // ── Line 1: Agent info (left) | Provider/Model (right) ──
    // Left: agent ID, project, cwd, role
    // Right: (provider-type) provider/model effort:level

    const line1Parts: (StyledText | string)[] = []

    if (!S.ob.complete) {
      // Not set up
      line1Parts.push(t`${fg('#555570')('  charon')}`)
      const pad1 = ' '.repeat(Math.max(1, termW - 50))
      line1Parts.push(pad1)
      line1Parts.push(t`${fg('#7a6f9a')(`Setup: ${S.ob.step}  ${S.ob.provider || 'no provider'}/${S.ob.model || 'no model'}`)}`)
    } else {
      // Set up — show agent + provider info
      const project = S.ob.project ? (S.ob.project as string).split('/').pop() || '' : ''
      const sessionId = (globalThis as any).__charonSessionId || ''
      const agentId = sessionId || (S.agents.find((a: any) => a.role === 'charon')?.id || '')
      const spec = (S.agents.find((a: any) => a.role === 'charon') as any)?.specialization || ''

      // Left side: agent info
      const leftItems: string[] = []
      if (agentId) leftItems.push(agentId)
      if (project) leftItems.push(project)
      if (spec) leftItems.push(`(${spec})`)
      line1Parts.push(t`${fg('#555570')(`  ${leftItems.join('  ')}`)}`)

      // Right side: provider type + provider/model + effort
      const icon = S.streaming ? '●' : '○'
      const effort = S.thinkingLevel || 'medium'
      const isApi = S.ob.provider === 'api'
      const providerLabel = isApi ? '(api)' : '(provider)'
      const providerColor = isApi ? '#b45309' : '#555570'

      const rightText = `${icon} ${S.ob.provider}/${S.ob.model}  effort:${effort}`
      const pad1 = ' '.repeat(Math.max(1, termW - 4 - leftItems.join('  ').length - rightText.length - providerLabel.length - 2))
      line1Parts.push(pad1)
      line1Parts.push(t`${fg(providerColor)(providerLabel)} `)
      line1Parts.push(t`${fg('#555570')(rightText)}`)
    }
    statusBar.content = joinStyled(...line1Parts)

    // ── Line 2: Tokens (left) | Hotkeys (right) ──
    const line2Parts: (StyledText | string)[] = []

    if (S.ob.complete) {
      // Left: heartbeat + background process indicators + token info
      const now = Date.now()
      const hbAge = now - S.lastHeartbeatTs
      // Heartbeat: ♡ that briefly fills to ♥
      const hbIcon = hbAge < 1500 ? '♥' : '♡'
      const hbColor = hbAge < 1500 ? '#4a3f6b' : '#2a2a3a'

      // Consolidation: 🧠 dims after 3s, gone after 6s
      const conAge = now - S.lastConsolidationTs
      const showCon = S.lastConsolidationTs && conAge < 6000
      const conColor = conAge < 3000 ? '#3d3d4f' : '#2a2a3a'

      // Autonomous: ⚡ dims after 3s, gone after 6s
      const autoAge = now - S.lastAutoTaskTs
      const showAuto = S.lastAutoTaskTs && autoAge < 6000
      const autoColor = autoAge < 3000 ? '#3d3d4f' : '#2a2a3a'

      line2Parts.push(t`${fg(hbColor)(` ${hbIcon}`)}`)

      // Agent mode + batch progress
      const modeColors: Record<string, string> = {
        'interactive': '#4a4a5e', 'autonomous': '#b45309',
        'delegating': '#6366f1', 'idle': '#3b3b4f',
      }
      const modeText = S.batchProgress
        ? `${S.agentMode} ${S.batchProgress}`
        : S.agentMode
      line2Parts.push(t`${fg(modeColors[S.agentMode] || '#4a4a5e')(` ${modeText}`)}`)

      if (showCon) line2Parts.push(t`${dim(fg(conColor)(' 🧠'))}`)
      if (showAuto) line2Parts.push(t`${dim(fg(autoColor)(' ⚡'))}`)
      const fmtTok = (n: number) => n >= 10000 ? `${(n/1000).toFixed(1)}k` : n >= 1000 ? `${(n/1000).toFixed(1)}k` : `${n}`
      const ctxColor = S.contextPct > 80 ? '#ef4444' : S.contextPct > 50 ? '#f59e0b' : '#4a4a5e'
      line2Parts.push(t`${fg('#4a4a5e')(` ↑${fmtTok(S.tokensIn)} ↓${fmtTok(S.tokensOut)}`)}`)
      line2Parts.push(t`${fg(ctxColor)(` ctx:${S.contextPct}%`)}`)
      if (S.showTimestamps) line2Parts.push(t`${fg('#4a4a5e')('  ⏱')}`)
    } else {
      line2Parts.push(t`${fg('#4a4a5e')('  type / for commands')}`)
    }

    // Right: view hotkeys + streaming hints
    let hotkeys = 'F1:chat  F2:dash  F3:sessions  Ctrl+P:info  Ctrl+Shift+C:copy'
    if (S.streaming) {
      hotkeys = 'Esc:abort  Enter:steer  /queue:follow-up'
    }
    const leftLen = S.ob.complete ? 30 : 22
    const pad2 = ' '.repeat(Math.max(1, termW - leftLen - hotkeys.length - 2))
    line2Parts.push(pad2)
    line2Parts.push(t`${fg(S.streaming ? '#b45309' : '#3b3b4f')(hotkeys)}`)

    statusBar2.content = joinStyled(...line2Parts)
    renderer.requestRender()
  }

  function switchView(view: ViewName) {
    S.view = view

    // Both chatScroll and sessScroll are always in root.
    // Toggle visible to show/hide. Never add/remove them.
    const newEl = view === 'chat' ? chatScroll : view === 'dashboard' ? dashboard.root : sessGrid.root
    if (newEl !== activeViewEl) {
      try { root.remove(activeViewEl.id) } catch {}
      root.add(newEl, 0)
      activeViewEl = newEl
    }

    if (view === 'sessions') {
      // Minimize bottom bar (can't hide — causes tmux collapse)
      input.placeholder = ''
      try { input.value = '' } catch {}
      inputBox.borderColor = '#0a0a12'  // match background = invisible border
      inputBox.backgroundColor = '#0a0a12'
      input.backgroundColor = '#0a0a12'
      input.textColor = '#0a0a12'  // hide cursor
      statusBar.content = t`${dim('  Tab:switch  ↑↓←→:navigate  Enter:connect  Esc:back')}`
      statusBar2.content = ''
      const liveState = (S as any)._liveState as Map<string, {status: string, summary: string}> | undefined
      SS.agents = (S.agents as any[]).map((a: any) => {
        const live = liveState?.get(a.id)
        if (live) return { ...a, status: live.status || a.status, last_summary: live.summary || a.last_summary }
        return a
      }) as any
      SS.projects = S.projects
      sessGrid.update()
    } else {
      // Restore input appearance
      input.placeholder = S.ob.complete ? 'Type a message or /command...' : 'Type /setup provider <name> to get started...'
      inputBox.borderColor = '#4b5563'
      inputBox.backgroundColor = undefined
      input.backgroundColor = '#0f172a'
      input.textColor = '#f8fafc'
    }

    rebuildView()
    updateStatus()
    if (view === 'chat') { input.focus() }
    else { input.blur() }  // blur input on dashboard/sessions so keys aren't eaten
    if (view !== 'chat') backend.sendRefresh()
  }

  // ── Paste handling ──────────────────────────────────────────────────────
  // Store pasted content and show markers in input (like pi-agent)
  let _pasteCounter = 0
  const _pastes = new Map<number, string>()

  renderer.keyInput.on('paste', (event: any) => {
    const text = event.text as string
    if (!text) return

    const lines = text.split('\n')
    const totalChars = text.length

    // Small paste (≤5 lines and ≤500 chars): let Input handle it normally
    if (lines.length <= 5 && totalChars <= 500) return

    // Large paste: store content and insert marker
    event.preventDefault()
    _pasteCounter++
    const pasteId = _pasteCounter
    _pastes.set(pasteId, text)

    const marker = lines.length > 5
      ? `[paste #${pasteId} +${lines.length} lines]`
      : `[paste #${pasteId} ${totalChars} chars]`

    try {
      const current = input.value || ''
      input.value = current + marker
    } catch {}
    renderer.requestRender()
  })

  // Expand paste markers before sending to backend
  function expandPasteMarkers(text: string): string {
    return text.replace(/\[paste #(\d+) [^\]]+\]/g, (match, id) => {
      const pasteId = parseInt(id, 10)
      return _pastes.get(pasteId) || match
    })
  }

  // ── Key handling ───────────────────────────────────────────────────────
  let _lastCtrlC = 0

  renderer.keyInput.on('keypress', (key: any) => {
    // Ctrl+C: first press clears input, second press (within 2s) exits
    if (key.name === 'c' && key.ctrl) {
      key.preventDefault()
      const now = Date.now()
      let inputText = ''
      try { inputText = (input.value || '').trim() } catch {}
      if (inputText) {
        // First: clear the input
        try { input.value = '' } catch {}
        _lastCtrlC = now
        return
      }
      if (now - _lastCtrlC < 2000) {
        // Double tap: exit
        _cleanExit(0)
      }
      _lastCtrlC = now
      // Show hint
      try {
        activityText.content = t`${dim('  Press Ctrl+C again to exit')}`
        activityBox.height = 1
        activityBox.maxHeight = undefined
        activityBox.overflow = undefined
        renderer.requestRender()
      } catch {}
      setTimeout(() => {
        try {
          if (Date.now() - _lastCtrlC >= 1900) {
            activityText.content = ''
            activityBox.height = 0
            activityBox.maxHeight = 0
            activityBox.overflow = 'hidden'
            renderer.requestRender()
          }
        } catch {}
      }, 2000)
      return
    }

    // Ctrl+Shift+C: copy selected text or last agent message to clipboard
    if (key.name === 'c' && key.ctrl && key.shift) {
      key.preventDefault()
      let text = ''
      // First try: get selected text from renderer
      try {
        const sel = renderer.getSelection()
        if (sel && sel.isActive) {
          text = sel.getSelectedText()
        }
      } catch {}
      // Fallback: copy last assistant message
      if (!text) {
        for (let i = chatMsgRenderables.length - 1; i >= 0; i--) {
          const r = chatMsgRenderables[i]
          if (r && r.content && typeof r.content === 'string' && r.content.trim()) {
            text = r.content
            break
          }
        }
      }
      if (text) {
        // OSC 52 clipboard (works over SSH too)
        const encoded = Buffer.from(text).toString('base64')
        process.stdout.write(`\x1b]52;c;${encoded}\x07`)
        // Also try native clipboard
        try {
          const { execSync } = require('child_process')
          if (process.platform === 'linux') {
            try { execSync('xclip -selection clipboard', { input: text, timeout: 2000, stdio: ['pipe', 'ignore', 'ignore'] }) }
            catch { try { execSync('xsel --clipboard --input', { input: text, timeout: 2000, stdio: ['pipe', 'ignore', 'ignore'] }) } catch {} }
          }
        } catch {}
        // Brief visual feedback
        const prevContent = activityText.content
        activityText.content = t`${fg('#86efac')('  ✓ Copied to clipboard')}`
        activityBox.height = 1; activityBox.maxHeight = undefined; activityBox.overflow = undefined
        renderer.requestRender()
        setTimeout(() => {
          activityText.content = prevContent || ''
          if (!prevContent) { activityBox.height = 0; activityBox.maxHeight = 0; activityBox.overflow = 'hidden' }
          renderer.requestRender()
        }, 1500)
      }
      return
    }

    // Sessions view: arrow keys navigate sidebar, not input
    if (S.view === 'sessions' && (key.name === 'up' || key.name === 'down' || key.name === 'left' || key.name === 'right' || key.name === 'return' || key.name === 'tab' || key.name === 'escape')) {
      key.preventDefault()
      // Let the sessions key handler below process it
    }

    // Scroll chat: Page Up/Down, Shift+Up/Down, or plain Up/Down (for mouse wheel)
    // Gnome-terminal converts scroll wheel to arrow keys in alt-screen without mouse tracking.
    if (S.view === 'chat' && !S.menuOpen) {
      const pageAmt = (process.stdout?.rows || 24) - 8
      if (key.name === 'pageup' || (key.name === 'up' && key.shift)) {
        chatScroll.scrollTo(Math.max(0, chatScroll.scrollTop - pageAmt))
        key.preventDefault()
        renderer.requestRender()
        return
      }
      if (key.name === 'pagedown' || (key.name === 'down' && key.shift)) {
        chatScroll.scrollTo(chatScroll.scrollTop + pageAmt)
        key.preventDefault()
        renderer.requestRender()
        return
      }
      // Plain arrow up/down: scroll by 3 lines (matches scroll wheel feel)
      if (key.name === 'up' && !key.ctrl && !key.meta) {
        chatScroll.scrollTo(Math.max(0, chatScroll.scrollTop - 3))
        key.preventDefault()
        renderer.requestRender()
        return
      }
      if (key.name === 'down' && !key.ctrl && !key.meta) {
        chatScroll.scrollTo(chatScroll.scrollTop + 3)
        key.preventDefault()
        renderer.requestRender()
        return
      }
    }

    // When entered into a tmux session, consume ALL keys before Input sees them
    // (except F-keys for view switching, Escape to exit, z to toggle zoom)
    if (SS.enteredSession && S.view === 'sessions') {

      if (key.name === 'f1') { SS.enteredSession = null; SS.zoomedSession = null; switchView('chat'); key.preventDefault(); return }
      if (key.name === 'f2') { SS.enteredSession = null; SS.zoomedSession = null; switchView('dashboard'); key.preventDefault(); return }
      if (key.name === 'f3') { key.preventDefault(); return } // already on sessions
      if (key.name === 'escape') {
        if (SS.zoomedSession) {
          SS.zoomedSession = null
        } else {
          SS.enteredSession = null
          ;(S as any)._enteredAgent = null
          ;(S as any)._steerInput = ''
          statusBar.content = t`${dim('  Tab:switch  ↑↓←→:navigate  Enter:connect  Esc:back')}`
        }
        rebuildView()
        key.preventDefault(); return
      }
      // Enter: send steer message to the entered session
      if (key.name === 'return') {
        let msg = ((S as any)._steerInput || '').trim()
        const agent = (S as any)._enteredAgent

        if (msg) {
          const agent = (S as any)._enteredAgent
          const targetSid = agent?.liveSessionId || ''
          const tmuxName = agent?.tmux_session || agent?.tmuxSession || ''
          if (targetSid) {
            // Live Charon session — send via file-based steer
            backend.send({ type: 'send_steer', target_session: targetSid, message: msg })
            statusBar.content = t`${fg('#22c55e')(`  📡 Sent: ${msg.slice(0, 50)}`)}`
            // Immediately add to conversation cache so it appears in the cell
            if (!(S as any)._convCache) (S as any)._convCache = new Map()
            const existing = (S as any)._convCache.get(targetSid) || ''
            ;(S as any)._convCache.set(targetSid, existing + `\n❯ ${msg}`)
            sessGrid.update()
            // Poll conversation immediately and repeatedly to catch the response
            const pollConv = () => backend.send({ type: 'live_conv', session_id: targetSid })
            setTimeout(pollConv, 500)
            setTimeout(pollConv, 1500)
            setTimeout(pollConv, 3000)
            setTimeout(pollConv, 5000)
            setTimeout(pollConv, 8000)
          } else if (tmuxName) {
            // Tmux session — send via tmux send-keys
            backend.sendTmuxSend(tmuxName, msg + '\n', false)
          }
          ;(S as any)._steerInput = ''
          sessGrid.update()
        }
        key.preventDefault(); return
      }
      // Ctrl+F toggles zoom — won't conflict with typing in the session
      if (key.name === 'f' && key.ctrl) {
        if (SS.zoomedSession) {
          SS.zoomedSession = null
        } else {
          SS.zoomedSession = SS.enteredSession
        }
        rebuildView()
        key.preventDefault(); return
      }

      // For live sessions (no tmux): handle Enter/Escape here, let other keys through to Input
      const enteredAgent = (S as any)._enteredAgent || S.agents.find((a: any) => a.id === SS.enteredSession)
      const isLiveSession = enteredAgent?.isLive || enteredAgent?.source === 'live'
      if (isLiveSession) {
        // Page Up/Down or Up/Down arrows: scroll the entered cell
        if (key.name === 'pageup' || key.name === 'up') {
          const allAg = (S.agents as any[]).filter((a: any) => a.role !== 'shade' && a.status !== 'stopped')
          const visAg = allAg.filter((a: any) => SS.visible.has(a.id))
          const cellIdx = visAg.findIndex((a: any) => a.id === SS.enteredSession)
          if (cellIdx >= 0 && cellIdx < sessGrid.gridScrolls.length && sessGrid.gridScrolls[cellIdx]) {
            const sc = sessGrid.gridScrolls[cellIdx]
            const amt = key.name === 'pageup' ? 10 : 3
            sc.scrollTo(Math.max(0, sc.scrollTop - amt))
          }
          key.preventDefault()
          return
        }
        if (key.name === 'pagedown' || key.name === 'down') {
          const allAg = (S.agents as any[]).filter((a: any) => a.role !== 'shade' && a.status !== 'stopped')
          const visAg = allAg.filter((a: any) => SS.visible.has(a.id))
          const cellIdx = visAg.findIndex((a: any) => a.id === SS.enteredSession)
          if (cellIdx >= 0 && cellIdx < sessGrid.gridScrolls.length && sessGrid.gridScrolls[cellIdx]) {
            const sc = sessGrid.gridScrolls[cellIdx]
            const amt = key.name === 'pagedown' ? 10 : 3
            sc.scrollTo(sc.scrollTop + amt)
          }
          key.preventDefault()
          return
        }
        if (key.name === 'backspace') {
          ;(S as any)._steerInput = ((S as any)._steerInput || '').slice(0, -1)
          sessGrid.update()
          key.preventDefault()
          return
        }
        if (key.name !== 'escape' && key.name !== 'return' && key.name !== 'f1' && key.name !== 'f2') {
          // Build typed text manually
          const ch = key.sequence || ''
          if (ch && ch.length === 1 && ch.charCodeAt(0) >= 32) {
            ;(S as any)._steerInput = ((S as any)._steerInput || '') + ch
            sessGrid.update()
          }
          key.preventDefault()
          return
        }
      }

      // For tmux sessions: forward everything to tmux
      const tmuxName = enteredAgent?.tmux_session || enteredAgent?.tmuxSession
      if (tmuxName) {
        let tmuxKey = ''
        if (key.name === 'return') tmuxKey = 'Enter'
        else if (key.name === 'backspace') tmuxKey = 'BSpace'
        else if (key.name === 'up') tmuxKey = 'Up'
        else if (key.name === 'down') tmuxKey = 'Down'
        else if (key.name === 'left') tmuxKey = 'Left'
        else if (key.name === 'right') tmuxKey = 'Right'
        else if (key.name === 'tab') tmuxKey = 'Tab'
        else if (key.name === 'space') { backend.sendTmuxKeys(tmuxName, ' ', true); key.preventDefault(); return }
        else if (key.ctrl && key.name === 'c') tmuxKey = 'C-c'
        else if (key.ctrl && key.name === 'd') tmuxKey = 'C-d'
        else if (key.ctrl && key.name === 'l') tmuxKey = 'C-l'
        else if (key.ctrl && key.name === 'a') tmuxKey = 'C-a'
        else if (key.ctrl && key.name === 'e') tmuxKey = 'C-e'
        else if (key.ctrl && key.name === 'u') tmuxKey = 'C-u'
        else if (key.ctrl && key.name === 'k') tmuxKey = 'C-k'
        else if (key.ctrl && key.name === 'w') tmuxKey = 'C-w'
        else if (key.sequence && key.sequence.length === 1 && !key.ctrl && !key.meta) {
          backend.sendTmuxKeys(tmuxName, key.sequence, true)
          key.preventDefault(); return
        }
        if (tmuxKey) {
          backend.sendTmuxKeys(tmuxName, tmuxKey, false)
        }
      }
      key.preventDefault(); return
    }

    if (key.name === 'f1') { switchView('chat'); return }
    if (key.name === 'f2') { switchView('dashboard'); return }
    if (key.name === 'f3') { switchView('sessions'); return }

    // Approval prompt handler (y/n/a when pending)
    if ((S as any)._approvalPending && (S as any)._approvalHandler) {
      const handled = (S as any)._approvalHandler(key)
      if (handled) return
    }

    // Ctrl+P: toggle info pane
    if (key.name === 'p' && key.ctrl && S.view === 'chat') {
      S.infoPaneOpen = !S.infoPaneOpen
      updateInfoPane()
      renderer.requestRender()
      key.preventDefault()
      return
    }

    // When info pane is focused: Tab cycles tabs, Escape closes
    if (S.infoPaneOpen && S.view === 'chat') {
      if (key.name === 'tab' && key.shift) {
        S.infoPaneTab = (S.infoPaneTab + 1) % 3
        updateInfoPane()
        key.preventDefault()
        return
      }
    }

    // Ctrl+T: toggle timestamps on messages
    if (key.name === 't' && key.ctrl) {
      S.showTimestamps = !S.showTimestamps
      rebuildView()
      updateStatus()
      return
    }

    // Escape: abort if streaming, close menu if open
    if (key.name === 'escape') {
      if (S.streaming) {
        backend.sendAbort()
        pushMsg(t`${fg('#ef4444')('  ⏹ Aborted')}`)
        S.streaming = false
        rebuildView()
        updateStatus()
        return
      }
      if (S.menuOpen) {
        S.menuOpen = false; (S as any)._pickerActive = false; (S as any)._lastPickerCmd = null; rebuildView(); return
      }
      return
    }

    // Menu navigation when open
    if (S.menuOpen && S.view === 'chat') {
      if (key.name === 'up') { S.menuIdx = Math.max(0, S.menuIdx - 1); rebuildView(); return }
      if (key.name === 'down') { S.menuIdx = Math.min(S.menuItems.length - 1, S.menuIdx + 1); rebuildView(); return }
      if (key.name === 'return') {
        const item = S.menuItems[S.menuIdx]
        if (item) {
          S.menuOpen = false; (S as any)._pickerActive = false; (S as any)._lastPickerCmd = null
          if (item.cmd.includes('<')) {
            // Has placeholder — put partial command in input for user to complete
            input.value = item.cmd.split('<')[0]
            rebuildView()
          } else {
            // Direct command — execute it
            input.value = ''
            addUserMessage(item.cmd)
            if (item.cmd === '/dashboard') { switchView('dashboard'); return }
            if (item.cmd === '/sessions') { switchView('sessions'); return }
            if (item.cmd === '/chat') { switchView('chat'); return }
            backend.sendCommand(item.cmd)
            rebuildView()
          }
        }
        return
      }
    }

    if (key.name === 'return' && S.view === 'chat') {
      const v = (input.value || '').trim()
      if (!v) return

      // While streaming: Enter = steer (interrupt), Ctrl+Enter or /queue = follow-up
      if (S.streaming && !v.startsWith('/')) {
        input.value = ''
        pushMsg(t`${bold(fg('#f59e0b')('steer> '))}${v}`)
        backend.sendSteer(v)
        rebuildView()
        return
      }

      // Always show what was typed
      addUserMessage(v)
      input.value = ''

      // /queue command — add to follow-up queue
      if (v.startsWith('/queue ')) {
        const msg = v.slice(7).trim()
        if (msg) {
          backend.sendFollowUp(msg)
          pushMsg(t`${dim(`  ⏳ Queued: ${msg}`)}`)
        }
        rebuildView()
        return
      }

      // Open full menu on bare /
      if (v === '/' || v === '/help' || v === '/?' || v === '/setup') {
        S.menuOpen = true
        S.menuItems = MENU_ITEMS
        S.menuIdx = 0
        rebuildView()
        return
      }

      // Filter menu on partial command
      if (v.startsWith('/') && v.length > 1) {
        const matches = MENU_ITEMS.filter(m => m.cmd.toLowerCase().startsWith(v.toLowerCase()))
        if (matches.length > 1) {
          S.menuOpen = true; S.menuItems = matches; S.menuIdx = 0; rebuildView(); return
        }
        if (matches.length === 1 && matches[0].cmd.includes('<')) {
          // Single match with placeholder — put in input
          input.value = matches[0].cmd.split('<')[0]
          rebuildView(); return
        }
      }

      if (v === '/dashboard' || v === '/dash') { switchView('dashboard'); return }
      if (v === '/sessions' || v === '/grid') { switchView('sessions'); return }
      if (v === '/chat') { switchView('chat'); return }
      // Commands that trigger pickers — set flag before sending so input handler doesn't race
      if (v === '/resume' || v === '/provider' || v === '/model' || v.startsWith('/setup model')) {
        ;(S as any)._pickerActive = true
      }
      if (v.startsWith('/')) backend.sendCommand(v)
      else { const expanded = expandPasteMarkers(v); S.streaming = true; S.streamStartTs = Date.now(); S.buf = []; startRowingAnimation(); backend.sendChat(expanded) }
      rebuildView(); updateStatus()
      return
    }

    if (S.view === 'dashboard') {
      const agents = filteredAgents(DS)
      if (key.name === 'tab') {
        DS.section = DS.section === 'agents' ? 'projects' : 'agents'
        rebuildView(); return
      }
      if (key.name === 'f' || key.name === 'F') {
        DS.agentFilter.shade = !DS.agentFilter.shade
        DS.agentIdx = 0; rebuildView(); return
      }
      if (key.name === 'up') {
        if (DS.section === 'agents' && DS.agentIdx > 0) DS.agentIdx--
        else if (DS.section === 'projects' && DS.projectIdx > 0) DS.projectIdx--
        rebuildView(); return
      }
      if (key.name === 'down') {
        if (DS.section === 'agents' && DS.agentIdx < agents.length - 1) DS.agentIdx++
        else if (DS.section === 'projects' && DS.projectIdx < S.projects.length - 1) DS.projectIdx++
        rebuildView(); return
      }
      if (key.name === 'return') {
        if (DS.section === 'agents' && agents[DS.agentIdx]) {
          const agent = agents[DS.agentIdx] as any
          const hasTmux = agent.hasTmux || false
          const isCharon = agent.role === 'charon' || agent.source === 'charon'

          if (hasTmux) {
            // Has live tmux — go to session grid with this agent highlighted
            switchView('sessions')
            // Find and select this agent in the grid
            const ga = getGridAgents(SS)
            const idx = ga.findIndex((a: any) => a.id === agent.id)
            if (idx >= 0) {
              SS.gridIdx = idx
              SS.section = 'grid'
              SS.enteredSession = agent.id
              input.blur()
              rebuildView()
            }
          } else {
            // No tmux — can't interact. Show guidance.
            pushMsg(joinStyled(
              t`${bold(fg('#a78bfa')(`  ┌─ Cannot connect to ${agent.name} ─`))}`, '\n',
              t`${fg('#a78bfa')('  │')}`, '\n',
              t`${fg('#a78bfa')('  │')} ${dim('This agent is not running in a tmux session.')}`, '\n',
              t`${fg('#a78bfa')('  │')} ${dim('To interact with it from Charon, either:')}`, '\n',
              t`${fg('#a78bfa')('  │')}`, '\n',
              t`${fg('#a78bfa')('  │')} ${bold('1.')} ${dim('Run it inside tmux:')}`, '\n',
              t`${fg('#a78bfa')('  │')}    ${fg('#22c55e')('tmux new -s my-agent "pi"')}`, '\n',
              t`${fg('#a78bfa')('  │')}`, '\n',
              t`${fg('#a78bfa')('  │')} ${bold('2.')} ${dim("Install charons-boat in your agent:")}`, '\n',
              t`${fg('#a78bfa')('  │')}    ${fg('#22c55e')('charons-boat wrap -- pi')}`, '\n',
              t`${fg('#a78bfa')('  │')}`, '\n',
              t`${fg('#a78bfa')('  │')} ${dim('github.com/dopppo/charons-boat')}`, '\n',
              t`${fg('#a78bfa')('  └' + '─'.repeat(45))}`,
            ))
            switchView('chat')
          }
        } else if (DS.section === 'projects') { switchView('sessions') }
        return
      }
    }

    if (S.view === 'sessions') {
      // Tab: cycle through agents → projects → grid
      if (key.name === 'tab') {
        if (SS.section === 'agents') SS.section = 'grid'
        else SS.section = 'agents'
        rebuildView(); return
      }

      const sidebarAgents = (S.agents as any[]).filter((a: any) => a.role !== 'shade' && a.status !== 'stopped')

      if (SS.section === 'agents') {
        if (key.name === 'up' && SS.agentIdx > 0) { SS.agentIdx--; sessGrid.update(); renderer.requestRender(); return }
        if (key.name === 'down' && SS.agentIdx < sidebarAgents.length - 1) { SS.agentIdx++; sessGrid.update(); renderer.requestRender(); return }
        if (key.name === 'return') {
          if (sidebarAgents[SS.agentIdx]) {
            const id = sidebarAgents[SS.agentIdx].id
            if (SS.visible.has(id)) SS.visible.delete(id)
            else SS.visible.add(id)
          }
          sessGrid.update()
          renderer.requestRender()
          return
        }
        return  // consume all keys when in agents section
      }

      if (SS.section === 'grid') {
        const allAgents = (S.agents as any[]).filter((a: any) => a.role !== 'shade' && a.status !== 'stopped')
        const ga = allAgents.filter((a: any) => SS.visible.has(a.id))

        if (SS.enteredSession) return

        const cols = 3  // matches our 3-column grid layout

        if (key.name === 'right' && SS.gridIdx < ga.length - 1) { SS.gridIdx++; rebuildView(); return }
        if (key.name === 'left' && SS.gridIdx > 0) { SS.gridIdx--; rebuildView(); return }
        if (key.name === 'down' && SS.gridIdx + cols < ga.length) { SS.gridIdx += cols; rebuildView(); return }
        if (key.name === 'up' && SS.gridIdx - cols >= 0) { SS.gridIdx -= cols; rebuildView(); return }
        if (key.name === 'return' && ga[SS.gridIdx]) {
          const agent = ga[SS.gridIdx] as any
          if (agent.source === 'virtual') {
            switchView('chat')
            return
          }
          // Enter session for interaction
          SS.enteredSession = ga[SS.gridIdx].id
          ;(S as any)._enteredAgent = agent
          // Don't use Input — track typed text manually
          ;(S as any)._steerInput = ''
          input.blur()
          statusBar.content = t`${fg('#22c55e')(`  ⏺ ${agent.name}`)}${dim('  Type and Enter to send  Esc:disconnect')}`
          rebuildView(); return
        }
        if (key.name === 'f' && key.ctrl && ga[SS.gridIdx]) {
          // Ctrl+F: zoom directly into a session (enter + zoom in one step)
          SS.enteredSession = ga[SS.gridIdx].id
          SS.zoomedSession = ga[SS.gridIdx].id
          input.blur()
          rebuildView(); return
        }
      }
    }

    // ── Live slash menu ──────────────────────────────────────────────────
    // After all key handling, check if input starts with / and update menu.
    // This runs on EVERY keypress in chat view to keep the menu in sync.
    if (S.view === 'chat') {
      setTimeout(() => {
        const val = (input.value || '').trim()
        if (val.startsWith('/') && val.length >= 1) {
          const matches = val === '/'
            ? MENU_ITEMS
            : MENU_ITEMS.filter(m => m.cmd.toLowerCase().startsWith(val.toLowerCase()))
          if (matches.length > 0) {
            // Commands that auto-trigger a backend picker on exact match
            const pickerCommands = ['/resume', '/provider', '/model']
            const exactMatch = matches.find(m => m.cmd.toLowerCase() === val.toLowerCase())
            if (exactMatch && pickerCommands.includes(val.toLowerCase())
                && !(S as any)._pickerActive && (S as any)._lastPickerCmd !== val) {
              ;(S as any)._pickerActive = true
              ;(S as any)._lastPickerCmd = val
              backend.sendCommand(val)
              // Don't show the static menu item — wait for the picker response
              return
            }
            // Find best match — exact or closest prefix
            let bestIdx = 0
            for (let i = 0; i < matches.length; i++) {
              if (matches[i].cmd.toLowerCase() === val.toLowerCase()) { bestIdx = i; break }
              if (matches[i].cmd.toLowerCase().startsWith(val.toLowerCase()) && 
                  matches[i].cmd.length < matches[bestIdx].cmd.length) bestIdx = i
            }
            S.menuOpen = true
            S.menuItems = matches
            // Only reset cursor if the menu items changed
            if (!S.menuOpen || S.menuItems.length !== matches.length) S.menuIdx = bestIdx
            rebuildView()
          } else {
            if (S.menuOpen && !(S as any)._pickerActive) { S.menuOpen = false; rebuildView() }
          }
        } else {
          if (S.menuOpen && !(S as any)._pickerActive) { S.menuOpen = false; rebuildView() }
        }
      }, 10)
    }
  })

  // ── Backend events ─────────────────────────────────────────────────────
  backend.onEvent((ev: BackendEvent) => {
    switch (ev.type) {
      case 'chat_delta': {
        // Clear any running animations
        if ((S as any)._thinkInterval) { clearInterval((S as any)._thinkInterval); (S as any)._thinkInterval = null }
        stopRowingAnimation()
        // If streamingMd was finalized (tool use turn), start a new one for post-tool response
        if (!streamingMd) {
          S.buf = []  // Reset buffer — previous text is already in the finalized MD
          startStreaming()
        }
        S.buf.push((ev.text as string) || '')
        updateStreaming(S.buf.join(''))
        renderer.requestRender()
        break
      }
      case 'thinking_start': {
        // Start a thinking animation
        const thinkIdx = S.msgs.length
        pushMsg(t`${fg('#7c3aed')('  ◆ thinking...')}`)
        rebuildView()
        const frames = ['◆', '◇', '◈', '◇']
        let frame = 0
        const thinkInterval = setInterval(() => {
          if (thinkIdx >= S.msgs.length || S.view !== 'chat') { clearInterval(thinkInterval); return }
          frame = (frame + 1) % frames.length
          S.msgs[thinkIdx] = { styled: t`${fg('#7c3aed')(`  ${frames[frame]} thinking...`)}` }
          rebuildView()
        }, 300)
        // Store interval so we can clear it when response arrives
        ;(S as any)._thinkInterval = thinkInterval
        break
      }
      case 'tool_call': {
        if ((S as any)._thinkInterval) { clearInterval((S as any)._thinkInterval); (S as any)._thinkInterval = null }
        // Finalize any in-progress streaming text before adding tool block
        if (streamingMd) {
          finishStreaming()
          S.buf = []
        }
        const nm = (ev.tool_name as string)||'', ar = ev.arguments as any
        const s = nm==='Bash'?(ar.command||'').slice(0,60):nm==='Read'?(ar.path||''):nm==='Write'?`${ar.path} (${(ar.content||'').length}ch)`:nm==='Edit'?(ar.path||''):JSON.stringify(ar).slice(0,60)

        // Tool-specific colors
        const toolColors: Record<string, {bg: string, fg: string, label: string}> = {
          'Read':  { bg: '#0d1a14', fg: '#6ee7b7', label: '📄' },
          'Write': { bg: '#1a1a0d', fg: '#fbbf24', label: '✏️' },
          'Edit':  { bg: '#1a140d', fg: '#f59e0b', label: '🔧' },
          'Bash':  { bg: '#0d0d1a', fg: '#93c5fd', label: '⚡' },
          'SpawnShade': { bg: '#1a0d1a', fg: '#c084fc', label: '👻' },
        }
        const tc = toolColors[nm] || { bg: '#151520', fg: '#a5b4fc', label: '⚙' }
        const w = Math.max(40, (process.stdout?.columns || 80) - 4)
        const header = ` ${tc.label} ${nm}  ${s}`
        const headerPad = header + ' '.repeat(Math.max(0, w - header.length))
        addToolBlock(joinStyled(t`${bold(fg(tc.fg)(bg(tc.bg)(headerPad.slice(0, w))))}`))

        // Show rowing animation in the activity indicator (above input bar)
        startRowingAnimation(tc)
        ;(S as any)._currentToolColor = tc
        rebuildView(); break
      }
      case 'tool_result': {
        stopRowingAnimation()
        const c = (ev.content as string) || '', e = ev.is_error as boolean
        const tc = (S as any)._currentToolColor || { bg: '#151520', fg: '#a5b4fc' }
        const w = Math.max(40, (process.stdout?.columns || 80) - 4)
        const errBg = '#1a0d0d'

        // Build entire tool result as ONE block
        const resultParts: (StyledText | string)[] = []
        const contentLines = c.split('\n').slice(0, 8)
        for (const l of contentLines) {
          const padded = '  ' + l.slice(0, w - 2) + ' '.repeat(Math.max(0, w - l.length - 2))
          resultParts.push(e
            ? t`${fg('#fca5a5')(bg(errBg)(padded.slice(0, w)))}`
            : t`${fg(tc.fg)(bg(tc.bg)(padded.slice(0, w)))}`
          )
          resultParts.push('\n')
        }
        if (c.split('\n').length > 8) {
          const moreLine = `  ... (${c.split('\n').length} lines total)` + ' '.repeat(Math.max(0, w - 30))
          resultParts.push(t`${dim(fg(tc.fg)(bg(tc.bg)(moreLine.slice(0, w))))}`)
        }
        addToolBlock(joinStyled(...resultParts))
        renderer.requestRender(); break
      }
      case 'turn_complete':
        stopRowingAnimation()
        if ((S as any)._thinkInterval) { clearInterval((S as any)._thinkInterval); (S as any)._thinkInterval = null }
        // streamingMd should already be finalized by tool_call handler, but just in case:
        if (streamingMd && S.buf.length) { finishStreaming(); S.buf=[] }
        rebuildView(); break
      case 'chat_complete':
        // Clear any lingering animations
        if ((S as any)._thinkInterval) { clearInterval((S as any)._thinkInterval); (S as any)._thinkInterval = null }
        stopRowingAnimation()
        if (S.buf.length) {
          finishStreaming()
        } else if (streamingMd) {
          try { chatScroll.remove(streamingMd.id) } catch {}
          streamingMd = null
        }
        S.buf=[]; S.streaming=false; stopRowingAnimation(); renderer.requestRender(); scrollToBottom(); updateStatus(); break
      case 'approval_request': {
        const tool = (ev.tool as string) || '?'
        const params = (ev.params as string) || ''
        const risk = (ev.risk as string) || 'unknown'
        const reason = (ev.reason as string) || ''
        const riskColor = risk === 'dangerous' ? '#ef4444' : risk === 'network' ? '#f59e0b' : '#6366f1'

        // Show approval box
        pushMsg(joinStyled(
          t`${fg('#7c3aed')(`╭─ Approval Required ${'─'.repeat(30)}╮`)}`, '\n',
          t`${fg('#7c3aed')('│')}`, '\n',
          t`${fg('#7c3aed')('│')} ${bold(`Tool: ${tool}`)}`, '\n',
          t`${fg('#7c3aed')('│')} ${fg(riskColor)(`Risk: ${risk}`)} — ${reason}`, '\n',
          params ? joinStyled(t`${fg('#7c3aed')('│')} ${dim(params)}`, '\n') : '',
          t`${fg('#7c3aed')('│')}`, '\n',
          t`${fg('#7c3aed')('│')} ${bold('y')}${dim(' = approve')}  ${bold('n')}${dim(' = deny')}  ${bold('a')}${dim(' = approve all for session')}`, '\n',
          t`${fg('#7c3aed')(`╰${'─'.repeat(45)}╯`)}`,
        ))
        rebuildView()

        // Set up a one-time key handler for the approval response
        const _origKeyHandler = (S as any)._approvalHandler
        ;(S as any)._approvalPending = true
        ;(S as any)._approvalHandler = (key: any) => {
          if (!((S as any)._approvalPending)) return false
          if (key.name === 'y' || key.name === 'Y') {
            (S as any)._approvalPending = false
            backend.send({ type: 'approval_response', approved: true })
            pushMsg(t`${fg('#22c55e')('  ✓ Approved')}`)
            rebuildView()
            return true
          }
          if (key.name === 'n' || key.name === 'N' || key.name === 'escape') {
            (S as any)._approvalPending = false
            backend.send({ type: 'approval_response', approved: false })
            pushMsg(t`${fg('#ef4444')('  ✗ Denied')}`)
            rebuildView()
            return true
          }
          if (key.name === 'a' || key.name === 'A') {
            (S as any)._approvalPending = false
            backend.send({ type: 'approval_response', approved: true })
            backend.sendCommand('/approve all')
            pushMsg(t`${fg('#22c55e')('  ✓ All tools approved for session')}`)
            rebuildView()
            return true
          }
          return false
        }
        break
      }
      case 'steer_queued':
        pushMsg(t`${fg('#f59e0b')(`  ⚡ Steering: ${(ev.message as string) || ''} (${ev.pending || 0} queued)`)}`)
        rebuildView(); break
      case 'follow_up_queued':
        pushMsg(t`${fg('#6366f1')(`  ⏳ Follow-up queued: ${(ev.message as string) || ''} (${ev.pending || 0} queued)`)}`)
        rebuildView(); break
      case 'steer_delivered':
        pushMsg(t`${fg('#f59e0b')(`  ⚡ Steer delivered (${ev.skipped_tools || 0} tools skipped)`)}`)
        rebuildView(); break
      case 'follow_up_delivered':
        pushMsg(t`${fg('#6366f1')(`  ⏳ Follow-up delivered`)}`)
        rebuildView(); break
      case 'suggestions': {
        const title = (ev.title as string) || 'Commands'
        const items = (ev.items as Array<{cmd: string, desc: string}>) || []
        const parts: (StyledText | string)[] = []
        parts.push(t`${bold(fg('#a78bfa')(`  ┌─ ${title} ─`))}`)
        for (const item of items) {
          parts.push('\n')
          // Don't use t`` to combine StyledText — use joinStyled
          parts.push(joinStyled(
            t`${fg('#a78bfa')('  │')} `,
            t`${bold(fg('#e2e8f0')(item.cmd))}`,
            '  ',
            t`${dim(item.desc)}`,
          ))
        }
        parts.push('\n')
        parts.push(t`${fg('#a78bfa')('  └' + '─'.repeat(40))}`)
        pushMsg(joinStyled(...parts))
        rebuildView(); break
      }
      case 'auth_url': {
        const url = (ev.url as string) || ''
        const provider = (ev.provider as string) || ''

        // Try to auto-open in browser
        try {
          const { execSync } = require('child_process')
          const opener = process.platform === 'darwin' ? 'open' : 'xdg-open'
          execSync(`${opener} "${url}"`, { stdio: 'ignore', timeout: 5000 })
        } catch {}

        // Copy to clipboard — try every method available
        let copied = false
        const { execSync, spawnSync } = require('child_process')
        const copyMethods = [
          () => { spawnSync('xclip', ['-selection', 'clipboard'], { input: url, timeout: 3000 }); return true },
          () => { spawnSync('xsel', ['--clipboard', '--input'], { input: url, timeout: 3000 }); return true },
          () => { spawnSync('wl-copy', [], { input: url, timeout: 3000 }); return true },
          () => { spawnSync('pbcopy', [], { input: url, timeout: 3000 }); return true },
        ]
        for (const method of copyMethods) {
          if (copied) break
          try { copied = method() } catch {}
        }
        // OSC 52 — works in kitty, alacritty, WezTerm, iTerm2
        if (!copied) {
          try {
            const b64 = Buffer.from(url).toString('base64')
            process.stderr.write(`\x1b]52;c;${b64}\x07`)
            copied = true
          } catch {}
        }
        // Also save to a temp file as last resort
        try {
          require('fs').writeFileSync('/tmp/charon-auth-url.txt', url + '\n')
        } catch {}

        const ap: (StyledText | string)[] = [
          t`${bold(fg('#a78bfa')('  ┌─ Authentication: ' + provider + ' ─'))}`, '\n',
          t`${fg('#a78bfa')('  │')}`, '\n',
          t`${fg('#a78bfa')('  │')} ${bold('A browser window should open automatically.')}`, '\n',
          t`${fg('#a78bfa')('  │')} ${dim(copied ? '✓ Link copied to clipboard.' : '→ Link saved to /tmp/charon-auth-url.txt')}`, '\n',
          t`${fg('#a78bfa')('  │')}`, '\n',
          t`${fg('#a78bfa')('  │')} ${dim('If browser didn\'t open, copy the URL below:')}`, '\n',
          t`${fg('#a78bfa')('  │')}`, '\n',
        ]
        // Show URL inside the box — wrap with consistent left padding
        const boxInner = Math.max(20, (process.stdout.columns || 80) - 10)
        let urlRem = url
        while (urlRem.length > 0) {
          ap.push(joinStyled(t`${fg('#a78bfa')('  │')}   `, t`${fg('#60a5fa')(urlRem.slice(0, boxInner))}`))
          ap.push('\n')
          urlRem = urlRem.slice(boxInner)
        }
        ap.push(t`${fg('#a78bfa')('  │')}`)
        ap.push('\n')
        ap.push(joinStyled(t`${fg('#a78bfa')('  │')} `, t`${dim('The browser will redirect back automatically.')}`))
        ap.push('\n')
        ap.push(joinStyled(t`${fg('#a78bfa')('  │')} `, t`${dim('Fallback: ')}`, t`${fg('#22c55e')('/setup auth-code <CODE>')}`))
        ap.push('\n')
        ap.push(t`${fg('#a78bfa')('  │')}`)
        ap.push('\n')
        ap.push(t`${fg('#a78bfa')('  └' + '─'.repeat(Math.min(54, boxInner + 2)) + '┘')}`)
        pushMsg(joinStyled(...ap))
        rebuildView()
        break
      }
      case 'live_conv': {
        const sid = (ev.session_id as string) || ''
        const preview = (ev.preview as string) || ''
        if (sid && preview) {
          if (!(S as any)._convCache) (S as any)._convCache = new Map()
          ;(S as any)._convCache.set(sid, preview)
          if (S.view === 'sessions') sessGrid.update()
        }
        break
      }
      case 'tmux_capture': {
        const session = (ev.session as string) || ''
        const content = (ev.content as string) || ''
        const captureState = (ev.state as string) || ''
        const captureSummary = (ev.summary as string) || ''
        // Find agent ID from tmux session name
        const agent = S.agents.find((a: any) => (a.tmux_session || a.tmuxSession) === session)
        const sess = S.sessions.find((s: any) => (s.tmuxSession || s.tmux_session) === session)
        const agentId = agent?.id || sess?.agentId || `tmux-${session}`
        SS.tmuxContent.set(agentId, content)
        // Update agent status and summary from live capture detection.
        // Store in a separate map so refresh data doesn't overwrite it.
        if (captureState || captureSummary) {
          if (!(S as any)._liveState) (S as any)._liveState = new Map()
          ;(S as any)._liveState.set(agentId, { status: captureState, summary: captureSummary })
        }
        // Update grid content directly — don't call rebuildView (causes tmux collapse)
        if (S.view === 'sessions') sessGrid.update()
        break
      }
      case 'model_picker': {
        const models = (ev.models as Array<{id: string, desc: string}>) || []
        const pickerType = (ev.provider as string) || ''
        S.menuOpen = true
        ;(S as any)._pickerActive = true  // prevent input change handler from closing
        if (pickerType === 'switch') {
          S.menuItems = models.map(m => ({ cmd: `/provider ${m.id}`, desc: m.desc }))
        } else if (pickerType === 'resume') {
          S.menuItems = models.map((m: any) => ({ cmd: `/resume ${m.id}`, desc: m.desc, age: m.age || '' }))
        } else {
          // Model picker
          S.menuItems = models.map(m => ({ cmd: `/setup model ${m.id}`, desc: m.desc }))
        }
        S.menuIdx = 0
        rebuildView()
        break
      }
      case 'conversation_restored': {
        const messages = (ev.messages as Array<any>) || []
        const count = (ev.count as number) || 0
        if (count === 0) break

        // Remove welcome text
        try { chatScroll.remove(welcomeText.id) } catch {}

        // Render saved messages — only user and assistant, skip tool results for brevity
        for (const msg of messages) {
          if (msg.role === 'user' && typeof msg.content === 'string' && msg.content.trim()) {
            addUserMessage(msg.content)
          } else if (msg.role === 'assistant' && typeof msg.content === 'string' && msg.content.trim()) {
            addCharonMessage(msg.content)
          }
          // Skip tool_result messages — they'd clutter the restored view
        }

        // Add a separator
        addStatusMessage(t`${dim(`  ── conversation resumed (${count} messages) ──`)}`)
        renderer.requestRender()
        break
      }
      case 'setup_complete': {
        // Clear chat renderables and show clean welcome
        S.msgs = []
        clearChatRenderables()
        const agent = (ev.agent as string) || ''
        const provider = (ev.provider as string) || ''
        const model = (ev.model as string) || ''
        pushMsg(t`${fg('#22c55e')('✓ Setup complete')}`)
        if (agent) pushMsg(t`${dim(`  Agent: ${agent}`)}`)
        pushMsg(t`${dim(`  Provider: ${provider}  Model: ${model}`)}`)
        pushMsg(t`${dim('  Type a message to start chatting.')}`)
        renderer.requestRender()
        updateStatus()
        backend.sendRefresh()
        break
      }
      case 'toggle_timestamps': {
        S.showTimestamps = !S.showTimestamps
        pushMsg(t`${yellow(`  Timestamps ${S.showTimestamps ? 'enabled ⏱' : 'disabled'}`)}`)
        rebuildView(); updateStatus(); break
      }
      case 'usage': {
        S.tokensIn += (ev.input_tokens as number) || 0
        S.tokensOut += (ev.output_tokens as number) || 0
        S.contextPct = (ev.context_pct as number) ?? S.contextPct
        updateStatus()
        renderer.requestRender()
        break
      }
      case 'status': (S as any)._pickerActive = false; pushMsg(t`${yellow(`  ${(ev.message as string)||''}`)}`); rebuildView(); updateStatus(); break
      case 'error': pushMsg(t`${red(`  Error: ${(ev.error as string)||''}`)}`); S.streaming=false; stopRowingAnimation(); rebuildView(); updateStatus(); break
      case 'refresh': {
        const p = ev.payload as any
        if (p?.onboarding) { S.ob = p.onboarding; input.placeholder = S.ob.complete ? 'Type a message or /command...' : 'Type /setup provider <name> to get started...' }
        if (p?.agent_mode) S.agentMode = p.agent_mode
        if (p?.batch_progress) S.batchProgress = p.batch_progress
        if (p?.session_id) (globalThis as any).__charonSessionId = p.session_id
        if (p?.session_info) { S.sessionInfo = p.session_info; updateInfoPane() }
        if (p?.agents) S.agents = p.agents
        if (p?.projects) S.projects = p.projects
        if (p?.sessions) S.sessions = p.sessions

        // Merge session data into agents list:
        // 1. Update existing Charon agents with live tmux status from sessions
        // 2. Add detected/tmux sessions that aren't Charon agents
        if (p?.sessions || p?.agents) {
          // Build session lookup by agentId
          const sessById = new Map<string, any>()
          for (const sess of S.sessions) {
            sessById.set((sess as any).agentId, sess)
          }

          // Update existing agents with tmux info
          for (const agent of S.agents as any[]) {
            const agentId = `session-${agent.id}`
            const sess = sessById.get(agentId) || sessById.get(agent.id)
            if (sess) {
              agent.hasTmux = sess.hasTmux || false
              agent.tmux_session = sess.tmuxSession || sess.tmux_session || agent.tmux_session || ''
              agent.tmuxSession = agent.tmux_session
              agent.source = agent.source || 'charon'
              if (sess.hasTmux && agent.status === 'stopped') {
                agent.status = 'running' // tmux is alive, agent is running
              }
            }
          }

          // Add new agents from detected/tmux sessions
          const knownIds = new Set((S.agents as any[]).map(a => a.id))
          for (const sess of S.sessions) {
            const sa = sess as any
            if (!knownIds.has(sa.agentId) && (sa.source === 'detected' || sa.source === 'tmux' || sa.source === 'live')) {
              S.agents.push({
                id: sa.agentId,
                name: sa.agentName,
                status: sa.status,
                role: sa.role || 'external',
                goal: sa.command || '',
                project: sa.project || '',
                mode: 'external',
                visibility: 'user',
                last_active: sa.lastActivity || '',
                tmux_session: sa.tmuxSession || sa.tmux_session || '',
                tmuxSession: sa.tmuxSession || sa.tmux_session || '',
                hasTmux: sa.hasTmux || false,
                source: sa.source,
                isLive: sa.isLive || false,
                liveSessionId: sa.liveSessionId || '',
                recent_actions: [],
                last_summary: '',
                memory_notes: 0,
              } as any)
              knownIds.add(sa.agentId)
            }
          }
        }
        if (p?.activity) {
          S.activity = p.activity
          for (const a of p.activity) {
            if (a.includes('heartbeat')) S.lastHeartbeatTs = Date.now()
            if (a.includes('consolidation_complete')) S.lastConsolidationTs = Date.now()
            if (a.includes('autonomous_task_created')) S.lastAutoTaskTs = Date.now()
          }
        }
        rebuildView()
        updateStatus(); break
      }
    }
  })

  // ── Layout ─────────────────────────────────────────────────────────────
  // Chat view: ScrollBox with mainText
  // Dashboard view: dashboard.root (real multi-column Box tree)
  // Sessions view: mainText with sessions content

  // chatScroll already created above (before functions that reference it)

  // Sessions uses its own scroll
  const sessText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const sessScroll = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%' })) as any
  sessScroll.add(sessText)

  // Root container that holds whichever view is active
  const root = instantiate(renderer, Box({ flexGrow: 1, width: '100%', height: '100%', flexDirection: 'column' })) as any
  root.add(chatScroll)
  // Only chatScroll starts in root. sessScroll added on F3 via switchView.

  let activeViewEl: any = chatScroll
  sessScroll.visible = false

  // Menu overlay text — sits above the input in the bottom bar
  const menuText = instantiate(renderer, Text({ content: '', width: '100%' })) as any

  const bottomBar = instantiate(renderer, Box({ position: 'absolute', bottom: 0, left: 0, width: '100%', flexDirection: 'column', backgroundColor: '#0a0a12' })) as any
  // Menu overlay (only has content when menu is open)
  const menuBox = instantiate(renderer, Box({ width: '100%', backgroundColor: '#0a0a12' })) as any
  menuBox.add(menuText)
  bottomBar.add(menuBox)
  // Activity indicator (rowing animation) — between menu and input, hidden by default
  const activityText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const activityBox = instantiate(renderer, Box({ width: '100%', backgroundColor: '#0a0a12', height: 0, maxHeight: 0, overflow: 'hidden' })) as any
  activityBox.add(activityText)
  bottomBar.add(activityBox)
  // Input
  const inputBox = instantiate(renderer, Box({ borderStyle: 'rounded', borderColor: '#4b5563', width: '100%', paddingLeft: 1, paddingRight: 1 })) as any
  inputBox.add(input)
  bottomBar.add(inputBox)
  // Status info below input
  const statusBox = instantiate(renderer, Box({ width: '100%', backgroundColor: '#0a0a12' })) as any
  statusBox.add(statusBar)
  const statusBox2 = instantiate(renderer, Box({ width: '100%', backgroundColor: '#0a0a12', paddingBottom: 1 })) as any
  statusBox2.add(statusBar2)
  bottomBar.add(statusBox)
  bottomBar.add(statusBox2)
  root.add(bottomBar)

  // ── Info pane (Ctrl+I) — absolute positioned, right side ──
  const infoPaneText = instantiate(renderer, Text({ content: '', width: '100%' })) as any
  const infoPaneScroll = instantiate(renderer, ScrollBox({ flexGrow: 1, width: '100%', stickyScroll: true, stickyStart: 'bottom' })) as any
  infoPaneScroll.add(infoPaneText)
  const infoPaneBox = instantiate(renderer, Box({
    position: 'absolute', right: 0, top: 0,
    width: 0,  // hidden by default
    height: '100%',
    flexDirection: 'column',
    backgroundColor: '#0c0c14',
    borderStyle: 'rounded',
    borderColor: '#3b3252',
    paddingLeft: 1,
    paddingRight: 1,
    overflow: 'hidden',
  })) as any
  infoPaneBox.add(infoPaneScroll)
  root.add(infoPaneBox)

  function updateInfoPane() {
    if (!S.infoPaneOpen || S.view !== 'chat') {
      infoPaneBox.width = 0
      return
    }
    const termW = renderer.terminalWidth || 80
    if (termW < 100) { infoPaneBox.width = 0; return }
    const paneW = Math.min(28, Math.floor(termW * 0.25))
    infoPaneBox.width = paneW

    const info = S.sessionInfo || {}
    const tasks = info.tasks || []
    const goals = info.goals || []
    const userModel = info.user_model || ''
    const tokens = info.tokens || {}
    const p: (StyledText | string)[] = []

    // Tab indicator
    const tabs = ['Tasks', 'Goals', 'Model']
    const tabParts: (StyledText | string)[] = []
    for (let i = 0; i < tabs.length; i++) {
      if (i > 0) tabParts.push(t`${dim('  ')}`)
      tabParts.push(i === S.infoPaneTab
        ? t`${bold(fg('#c4b5fd')(`[${tabs[i]}]`))}`
        : t`${dim(tabs[i])}`
      )
    }
    p.push(joinStyled(...tabParts))
    p.push('\n')
    p.push(t`${fg('#3b3252')('─'.repeat(paneW - 4))}`)
    p.push('\n')

    if (S.infoPaneTab === 0) {
      // Tasks tab
      if (tasks.length === 0) {
        p.push(t`${dim('No tasks yet.')}`)
        p.push('\n')
        p.push(t`${dim('Start a conversation')}`)
        p.push('\n')
        p.push(t`${dim('to see tasks here.')}`)
      } else {
        for (let i = tasks.length - 1; i >= 0; i--) {
          const task = tasks[i]
          const ts = task.ts ? new Date(task.ts * 1000) : null
          const time = ts ? `${ts.getHours().toString().padStart(2,'0')}:${ts.getMinutes().toString().padStart(2,'0')}` : ''
          const icon = '✓'
          const summary = (task.summary || task.instruction || '').slice(0, paneW - 8)
          const tools = task.tool_calls ? `${task.tool_calls}t` : ''
          const turns = task.turns ? `${task.turns}↻` : ''
          p.push(t`${fg('#6ee7b7')(`${icon} ${time}`)}`)
          if (tools || turns) p.push(t`${dim(` ${tools} ${turns}`)}`)
          p.push('\n')
          // Wrap summary
          let rem = summary
          while (rem.length > 0) {
            p.push(t`${dim(`  ${rem.slice(0, paneW - 6)}`)}`)
            p.push('\n')
            rem = rem.slice(paneW - 6)
          }
        }
      }
    } else if (S.infoPaneTab === 1) {
      // Goals tab
      if (goals.length === 0) {
        p.push(t`${dim('No goals detected.')}`)
      } else {
        for (const g of goals) {
          const statusIcon: Record<string, string> = {
            'active': '●', 'backlog': '○', 'proposed': '◆',
            'confirmed': '◉', 'completed': '✓', 'failed': '✗',
          }
          const statusColor: Record<string, string> = {
            'active': '#22c55e', 'backlog': '#6b7280', 'proposed': '#a78bfa',
            'confirmed': '#60a5fa', 'completed': '#6ee7b7', 'failed': '#ef4444',
          }
          const icon = statusIcon[g.status] || '○'
          const color = statusColor[g.status] || '#6b7280'
          p.push(t`${fg(color)(`${icon} ${g.title.slice(0, paneW - 6)}`)}`)
          p.push('\n')
          const meta: string[] = [`[${g.status}]`]
          if (g.scope) meta.push(g.scope)
          if (g.criteria?.length) meta.push(`${g.criteria.length} criteria`)
          if (g.intent_type === 'proposed') meta.push('/confirm')
          p.push(t`${dim(`  ${meta.join(' ')}`)}`)
          p.push('\n')
        }
      }
    } else {
      // User Model tab
      if (!userModel) {
        p.push(t`${dim('No user model yet.')}`)
        p.push('\n')
        p.push(t`${dim('Charon learns your')}`)
        p.push('\n')
        p.push(t`${dim('preferences over time.')}`)
      } else {
        // Strip the ═══ delimiter lines, render content
        for (const line of userModel.split('\n')) {
          if (/^═+$/.test(line.trim())) continue
          p.push(t`${fg('#d4c4a8')(line.slice(0, paneW - 4))}`)
          p.push('\n')
        }
      }
    }

    // Token footer
    p.push('\n')
    p.push(t`${fg('#3b3252')('─'.repeat(paneW - 4))}`)
    p.push('\n')
    const fmtK = (n: number) => n >= 1000 ? `${(n/1000).toFixed(1)}k` : `${n}`
    p.push(t`${dim(`chat: ${fmtK(tokens.chat_in || S.tokensIn)}↑ ${fmtK(tokens.chat_out || S.tokensOut)}↓`)}`)
    if (tokens.consolidation_tokens) {
      p.push('\n')
      p.push(t`${dim(`bg: ~${fmtK(tokens.consolidation_tokens)} consol`)}`)
    }
    p.push('\n')
    p.push(t`${fg('#3b3252')('─'.repeat(paneW - 4))}`)
    p.push('\n')
    p.push(t`${dim('Shift+Tab: switch tab')}`)
    p.push('\n')
    p.push(t`${dim('Ctrl+P: hide/show')}`)

    infoPaneText.content = joinStyled(...p)
  }

  renderer.root.add(root)

  // ── Start ──────────────────────────────────────────────────────────────
  rebuildView()
  updateStatus()
  input.focus()  // Focus input on startup so user can type immediately
  updateInfoPane()  // Show info pane on startup

  await backend.start()
  backend.sendRefresh()
  // Periodic agent/session discovery scan.
  // Always runs so new tmux sessions, local agents, and remote links
  // are discovered regardless of which view you're on.
  let lastRefresh = 0
  let hbCycle = 0
  setInterval(() => {
    const now = Date.now()

    // TUI heartbeat: brief pulse every 4 seconds (set timestamp, let it age naturally)
    hbCycle++
    if (hbCycle % 4 === 0) {
      S.lastHeartbeatTs = now
    }
    try { updateStatus() } catch {}

    // Dashboard/sessions: refresh every 3s. Chat: every 10s.
    const interval = S.view === 'chat' ? 10000 : 3000
    if (now - lastRefresh >= interval) {
      lastRefresh = now
      backend.sendRefresh()
    }
  }, 1000)

  // Poll tmux captures for visible sessions in the grid
  // Slower polling to avoid layout collapse in tmux environments
  let lastSlowPoll = 0
  let lastFastPoll = 0
  setInterval(() => {
    if (S.view !== 'sessions') return

    const now = Date.now()
    const ga = getGridAgents(SS)

    for (const a of ga) {
      const tmux = (a as any).tmux_session || (a as any).tmuxSession || ''
      const sess = S.sessions.find((s: any) => s.agentId === a.id)
      const tmuxName = tmux || (sess as any)?.tmuxSession || (sess as any)?.tmux_session || ''
      if (!tmuxName) continue

      if (a.id === SS.enteredSession && now - lastFastPoll >= 500) {
        backend.sendTmuxCapture(tmuxName)
        lastFastPoll = now
      } else if (now - lastSlowPoll >= 3000) {
        backend.sendTmuxCapture(tmuxName)
      }
    }

    // Poll live Charon session conversations
    if (now - lastSlowPoll >= 3000) {
      lastSlowPoll = now
      // Check all agents AND all sessions for live ones
      const liveIds = new Set<string>()
      for (const a of (S.agents as any[])) {
        const sid = a.liveSessionId || ''
        if (sid && (a.isLive || a.source === 'live')) liveIds.add(sid)
      }
      for (const s of S.sessions) {
        const sa = s as any
        if (sa.liveSessionId && sa.source === 'live') liveIds.add(sa.liveSessionId)
      }
      for (const sid of liveIds) {
        backend.send({ type: 'live_conv', session_id: sid })
      }
    }
    // Fast poll for entered live session (every 500ms)
    if (SS.enteredSession && now - lastFastPoll >= 500) {
      const ea = (S as any)._enteredAgent
      if (ea?.liveSessionId) {
        backend.send({ type: 'live_conv', session_id: ea.liveSessionId })
        lastFastPoll = now
      }
    }
  }, 500)
}

// Global ref to renderer for cleanup
let _renderer: any = null

const _cleanExit = (code = 0) => {
  try {
    // Disable mouse tracking FIRST (before anything else)
    process.stdout.write(
      '\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l'  // disable all mouse tracking
    )
    // Let the renderer clean up properly (exits alt screen, restores terminal)
    if (_renderer) {
      _renderer.destroy()
      _renderer = null
    }
    // Belt and suspenders: ensure terminal is fully reset after destroy
    process.stdout.write(
      '\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l' +  // mouse tracking (again, in case destroy re-enabled)
      '\x1b[?25h' +    // show cursor
      '\x1b[?2004l' +  // disable bracketed paste
      '\x1b[0m'        // reset attributes
    )
    // Exit raw mode
    if (process.stdin.setRawMode) process.stdin.setRawMode(false)
    process.stdin.unref()
  } catch {}
  // Print resume message to normal screen buffer (after alt screen exit)
  try {
    const sessionId = (globalThis as any).__charonSessionId
    const msgCount = (globalThis as any).__charonMsgCount || 0
    if (sessionId && msgCount > 0) {
      process.stdout.write(`\n  To resume this conversation: charon --resume=${sessionId}\n\n`)
    }
  } catch {}
  process.exit(code)
}

// Trap SIGINT so Bun doesn't kill the process — our keypress handler manages Ctrl+C
process.on('SIGINT', () => { /* swallowed — handled by keypress */ })


main().catch((err) => {
  try {
    process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l')
    if (_renderer) { _renderer.destroy(); _renderer = null }
    process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?25h\x1b[?2004l\x1b[0m')
    if (process.stdin.setRawMode) process.stdin.setRawMode(false)
    process.stdin.unref()
  } catch {}
  process.stderr.write(`Charon TUI error: ${err?.message||err}\n${err?.stack||''}\n`)
  process.exit(1)
})
