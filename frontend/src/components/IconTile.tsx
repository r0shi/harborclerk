import { ReactNode } from 'react'
import type { AreaHue } from '../hooks/useAreaAccent'

interface IconTileProps {
  /** Area name whose accent variables drive the tint. Falls back to the active area when omitted. */
  hue?: AreaHue
  /** Glyph or small element rendered inside (e.g. "📕" or "🔑"). */
  children: ReactNode
  /** Tile size in pixels. Default 30. */
  size?: number
}

/**
 * Small rounded-square colored tile used as a category/type indicator
 * (document type next to a doc title, settings-section icon, etc.).
 *
 * Uses --area-{hue}-accent-tint as the background and --area-{hue}-accent-text
 * as the foreground so dark/light variants come along for free. When hue
 * is omitted the active area's --area-accent-tint / --area-accent-text are
 * used instead (so the tile matches the page it lives on).
 */
export function IconTile({ hue, children, size = 30 }: IconTileProps) {
  const prefix = hue ? `--area-${hue}-accent` : '--area-accent'
  const style = {
    backgroundColor: `var(${prefix}-tint)`,
    color: `var(${prefix}-text)`,
    width: size,
    height: size,
    fontSize: Math.round(size * 0.47),
  }

  return (
    <div className="flex shrink-0 items-center justify-center rounded-[7px]" style={style} aria-hidden="true">
      {children}
    </div>
  )
}
