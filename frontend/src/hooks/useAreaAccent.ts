import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

/** Single source of truth for the eight area names used throughout the design system. */
export type AreaHue = 'ask' | 'research' | 'folders' | 'docs' | 'explore' | 'search' | 'observatory' | 'settings'

const ROUTE_TO_AREA: { test: (path: string) => boolean; area: AreaHue }[] = [
  // Order matters — most specific first.
  { test: (p) => p.startsWith('/research'), area: 'research' },
  { test: (p) => p.startsWith('/folders'), area: 'folders' },
  { test: (p) => p.startsWith('/docs'), area: 'docs' },
  { test: (p) => p.startsWith('/explore'), area: 'explore' },
  { test: (p) => p.startsWith('/search'), area: 'search' },
  { test: (p) => p.startsWith('/stats'), area: 'observatory' },
  {
    test: (p) => p.startsWith('/admin') || p.startsWith('/integrations') || p.startsWith('/preferences'),
    area: 'settings',
  },
  // Ask is the default — matches '/' and '/c/:conversationId'.
  { test: () => true, area: 'ask' },
]

/**
 * Returns the active area name for the current route.
 * Side effect: applies the area's accent vars to the layout root element
 * (the element with `data-layout-root`), which makes `--area-accent`,
 * `--area-accent-tint`, `--area-accent-text` resolve to the right hue.
 */
export function useAreaAccent(): { area: AreaHue } {
  const location = useLocation()
  const area = ROUTE_TO_AREA.find((m) => m.test(location.pathname))!.area

  useEffect(() => {
    const root = document.querySelector<HTMLElement>('[data-layout-root]')
    if (!root) return
    root.style.setProperty('--area-accent', `var(--area-${area}-accent)`)
    root.style.setProperty('--area-accent-tint', `var(--area-${area}-accent-tint)`)
    root.style.setProperty('--area-accent-text', `var(--area-${area}-accent-text)`)
  }, [area])

  return { area }
}
