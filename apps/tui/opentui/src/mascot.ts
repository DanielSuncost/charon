/**
 * Charon mascot renderer for OpenTUI.
 *
 * Renders the wraith sprite as a single character grid with the title
 * composited into it — exactly like the curses/textual versions.
 *
 * Three variants based on terminal size:
 *
 * FULL (≥95 cols, ≥30 rows):
 *   Sprite at 1.0× scale. The big CHARON title is already baked into the
 *   sprite cells at rows 16-19 (under the wraith's outstretched arm).
 *
 * MID (≥60 cols, ≥18 rows):
 *   Sprite at 0.55× scale. The baked-in big title gets garbled at this
 *   scale, so we:
 *   1. Render the sprite at 0.55×
 *   2. Blank out the title region (scaled coordinates)
 *   3. Stamp the mid-size block title (title_ascii_mid.txt) at that position
 *   Only the title characters are stamped — no black rectangle, so the
 *   lantern/robe pixels behind the title gaps show through.
 *
 * TINY (< 60 cols or < 18 rows):
 *   No sprite. Ornamental one-line title: ━━━ ❈ CHARON ❈ ━━━
 *
 * Colors: Raw sprite RGB (same as Textual version) for natural warm light.
 */

import { fg, bold, dim, type StyledText, StyledText as StyledTextClass } from '@opentui/core'
import type { TextChunk } from '@opentui/core'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'

const ASSETS_DIR = resolve(dirname(new URL(import.meta.url).pathname), '../../../../assets')

interface SpriteCell { x: number; y: number; ch: string; fg?: [number, number, number] }
interface SpriteData { width: number; height: number; cells: SpriteCell[] }
interface MascotConfig {
  sprite_path: string
  render?: { scale_x?: number; scale_y?: number }
  tiny_title?: { path: string; fg: [number, number, number] }
  tiny_title_source?: { x: number; y: number; w: number; h: number }
}

function loadJSON<T>(p: string, fb: T): T { try { return JSON.parse(readFileSync(p, 'utf-8')) } catch { return fb } }
function loadLines(p: string): string[] { try { return readFileSync(p, 'utf-8').split('\n') } catch { return [] } }
function h2(n: number) { return Math.max(0, Math.min(255, Math.floor(n))).toString(16).padStart(2, '0') }
function rgbHex(r: number, g: number, b: number) { return `#${h2(r)}${h2(g)}${h2(b)}` }

export type MascotVariant = 'full' | 'mid' | 'tiny'

export function chooseVariant(w: number, h: number): MascotVariant {
  if (w >= 95 && h >= 30) return 'full'
  if (w >= 60 && h >= 18) return 'mid'
  return 'tiny'
}

export interface MascotRender {
  styled: StyledText
  height: number
  variant: MascotVariant
}

/**
 * Build the mascot StyledText for the given terminal dimensions.
 * Call this on startup AND on every resize.
 */
export function renderMascot(termWidth: number, termHeight: number): MascotRender {
  const variant = chooseVariant(termWidth, termHeight)

  if (variant === 'tiny') {
    return buildTiny()
  }

  const config = loadJSON<MascotConfig>(resolve(ASSETS_DIR, 'mascot_config.json'), {
    sprite_path: 'assets/lantern_wraith_terminal_sprite_v2.json',
  })
  const sprite = loadJSON<SpriteData>(
    resolve(ASSETS_DIR, '..', config.sprite_path),
    { width: 0, height: 0, cells: [] },
  )
  if (!sprite.cells.length) return buildTiny()

  const scaleX = variant === 'full' ? 1.0 : 0.55
  const scaleY = variant === 'full' ? 1.0 : 0.55
  const cols = Math.min(termWidth, Math.max(1, Math.floor(sprite.width * scaleX)))
  const rows = Math.min(termHeight - 6, Math.max(1, Math.floor(sprite.height * scaleY)))

  // Build character + color grid from sprite
  const chars: string[][] = Array.from({ length: rows }, () => Array(cols).fill(' '))
  const colors: (string | null)[][] = Array.from({ length: rows }, () => Array(cols).fill(null))

  // Title region in sprite coordinates (where the big title is baked in)
  const titleSrc = config.tiny_title_source || { x: 10, y: 16, w: 54, h: 4 }

  for (const cell of sprite.cells) {
    const x = Math.floor(cell.x * scaleX)
    const y = Math.floor(cell.y * scaleY)
    if (x < 0 || x >= cols || y < 0 || y >= rows) continue
    const ch = (cell.ch || ' ').charAt(0)

    if (variant === 'mid') {
      // For mid: skip cells in the big title region (they'll be garbled at 0.55×)
      if (cell.x >= titleSrc.x && cell.x < titleSrc.x + titleSrc.w &&
          cell.y >= titleSrc.y && cell.y < titleSrc.y + titleSrc.h) {
        continue  // leave as space — will be overlaid with mid title
      }
    }

    if (ch === ' ' && !cell.fg) continue
    chars[y][x] = ch
    if (cell.fg) colors[y][x] = rgbHex(cell.fg[0], cell.fg[1], cell.fg[2])
  }

  // For mid variant: stamp the mid-size title at the scaled title position
  if (variant === 'mid') {
    const midTitlePath = config.tiny_title?.path
      ? resolve(ASSETS_DIR, '..', config.tiny_title.path)
      : resolve(ASSETS_DIR, 'title_ascii_mid.txt')
    const midLines = loadLines(midTitlePath).filter(l => l.length > 0)
    const titleFg = config.tiny_title?.fg ?? [176, 146, 62]
    const titleColor = rgbHex(titleFg[0], titleFg[1], titleFg[2])

    // Scaled position of title region
    const stampX = Math.floor(titleSrc.x * scaleX)
    const stampY = Math.floor(titleSrc.y * scaleY)

    for (let dy = 0; dy < midLines.length; dy++) {
      const line = midLines[dy]
      const y = stampY + dy
      if (y < 0 || y >= rows) continue
      // Spread the characters across the available width
      const lineChars = [...line]  // handle unicode properly
      for (let dx = 0; dx < lineChars.length; dx++) {
        const x = stampX + dx
        if (x < 0 || x >= cols) continue
        const ch = lineChars[dx]
        if (ch === ' ') continue  // DON'T stamp spaces — let sprite show through
        chars[y][x] = ch
        colors[y][x] = titleColor
      }
    }
  }

  // Add subtitle below sprite
  const subtitleY = rows  // would be below, but we embed it in the grid
  // Actually we'll append it as a separate line after the sprite

  // Trim trailing empty rows
  let lastRow = rows - 1
  while (lastRow > 0 && chars[lastRow].every((c, x) => c === ' ' && !colors[lastRow][x])) lastRow--
  const contentRows = lastRow + 1

  // Build StyledText — run-length encode by color per line
  const chunks: TextChunk[] = []
  for (let y = 0; y < contentRows; y++) {
    if (y > 0) chunks.push({ __isChunk: true, text: '\n' })
    let lineEnd = cols - 1
    while (lineEnd > 0 && chars[y][lineEnd] === ' ' && !colors[y][lineEnd]) lineEnd--
    lineEnd++
    let x = 0
    while (x < lineEnd) {
      const color = colors[y][x]
      let buf = chars[y][x]; x++
      while (x < lineEnd && colors[y][x] === color) { buf += chars[y][x]; x++ }
      chunks.push(color ? fg(color)(buf) : fg('#1a1a1a')(buf))
    }
  }

  // Append subtitle
  chunks.push({ __isChunk: true, text: '\n' })
  chunks.push(fg('#786446')('  Agent Operating System'))

  if (!chunks.length) return buildTiny()
  return { styled: new StyledTextClass(chunks), height: contentRows + 1, variant }
}

function buildTiny(): MascotRender {
  const config = loadJSON<MascotConfig>(resolve(ASSETS_DIR, 'mascot_config.json'), { sprite_path: '' })
  const titleFg = config.tiny_title?.fg ?? [176, 146, 62]
  const color = rgbHex(titleFg[0], titleFg[1], titleFg[2])
  const chunks: TextChunk[] = [
    fg('#5a4428')('━━━ '),
    bold(fg(color)('❈ CHARON ❈')),
    fg('#5a4428')(' ━━━'),
    { __isChunk: true, text: '\n' },
    fg('#786446')('  Agent Operating System'),
  ]
  return { styled: new StyledTextClass(chunks), height: 2, variant: 'tiny' }
}
