/**
 * Backend bridge — spawns the Python backend and communicates via
 * newline-delimited JSON over stdio.
 *
 * For the chat view, we use a DIRECT connection to the conversation engine
 * (no daemon polling). The backend process runs the engine in-process and
 * streams events back to the TUI.
 */

import { resolve, dirname } from 'node:path'

const ROOT = resolve(dirname(new URL(import.meta.url).pathname), '../../../..')
const BACKEND_SCRIPT = resolve(ROOT, 'apps/tui/opentui/chat_backend.py')

export interface BackendEvent {
  type: string
  request_id?: string
  [key: string]: unknown
}

export type EventHandler = (event: BackendEvent) => void

export class Backend {
  private proc: ReturnType<typeof Bun.spawn> | null = null
  private handlers: EventHandler[] = []
  private buffer = ''
  private requestId = 0

  async start() {
    this.proc = Bun.spawn({
      cmd: ['python3', BACKEND_SCRIPT],
      cwd: ROOT,
      stdin: 'pipe',
      stdout: 'pipe',
      stderr: 'pipe',
    })

    // Read stdout line by line
    const reader = this.proc.stdout?.getReader()
    if (!reader) return

    const decoder = new TextDecoder()
    ;(async () => {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        this.buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = this.buffer.indexOf('\n')) >= 0) {
          const line = this.buffer.slice(0, idx).trim()
          this.buffer = this.buffer.slice(idx + 1)
          if (!line) continue
          try {
            const event = JSON.parse(line) as BackendEvent
            for (const handler of this.handlers) {
              handler(event)
            }
          } catch {
            // ignore parse errors
          }
        }
      }
    })()

    // Log stderr
    const errReader = this.proc.stderr?.getReader()
    if (errReader) {
      const errDecoder = new TextDecoder()
      ;(async () => {
        while (true) {
          const { done, value } = await errReader.read()
          if (done) break
          const text = errDecoder.decode(value, { stream: true })
          if (text.trim()) {
            // Could log to a debug panel later
          }
        }
      })()
    }
  }

  onEvent(handler: EventHandler) {
    this.handlers.push(handler)
  }

  send(message: Record<string, unknown>) {
    if (!this.proc?.stdin) return
    const id = `r${++this.requestId}`
    this.proc.stdin.write(JSON.stringify({ ...message, request_id: id }) + '\n')
    return id
  }

  sendChat(text: string) {
    return this.send({ type: 'chat', message: text })
  }

  sendCommand(command: string) {
    return this.send({ type: 'command', command })
  }

  sendRefresh() {
    return this.send({ type: 'refresh' })
  }

  sendTmuxCapture(sessionName: string) {
    return this.send({ type: 'tmux_capture', session: sessionName })
  }

  sendTmuxKeys(sessionName: string, keys: string, literal = false) {
    return this.send({ type: 'tmux_send', session: sessionName, keys, literal })
  }

  sendSteer(text: string) {
    return this.send({ type: 'steer', message: text })
  }

  sendFollowUp(text: string) {
    return this.send({ type: 'follow_up', message: text })
  }

  sendAbort() {
    return this.send({ type: 'abort' })
  }

  sendAgentLedger(agentId?: string) {
    return this.send({ type: 'agent_ledger', agent_id: agentId || '' })
  }

  sendTaskDetail(taskId: string) {
    return this.send({ type: 'task_detail', task_id: taskId })
  }

  stop() {
    this.proc?.kill()
    this.proc = null
  }
}
